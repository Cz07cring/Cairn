#!/usr/bin/env python3
"""检查工具注册的「必填声明」与「静默填空」是否一致。

为什么有这个检查（2026-09-13，Issue #68/#70）
------------------------------------------------
实测失败链的**前两环都在代码里**：

  ① 工具的 `parameters` 未按上游约定标 `required: true`
     ⇒ 发给模型的 JSON Schema 里该字段是**可选**的
     ⇒ 模型省略它是**对 schema 的合理读法**；
  ② `buildArguments` 用 `typeof x === 'string' ? x : ''` 把「缺失」替换成 `''`
     ⇒ **抹掉「你忘了给」与「你给了空的」的区别** —— 而这正是模型自纠所需的信息；
  ③ 于是校验器只报 `EXECUTE_TOOL_ARGUMENTS_EMPTY_PATH`（不指名缺哪个字段、不说期望形状）
     ⇒ 模型原样重试；
  ④ 守卫判「无视提醒」⇒ 硬停（实测占全部轮次 ~10%、占失败 ~43%）。

上游的必填约定（`packages/core/agent-loop` 依赖的 tools schema 编译器）：
属性上写 `required: true` 才会进入 JSON Schema 的 `required[]`；
**不写就是可选**，不是「默认必填」。

本检查做静态比对：凡「被 `buildArguments` 以默认值兜底的字段」都**必须**标 `required: true`。
两者不一致 ⇒ 退出码 1（失败关闭），并指名字段与行号。

用法：
    python scripts/check_tool_param_required.py            # 检查
    python scripts/check_tool_param_required.py --list     # 只列工具与字段
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "apps/runner/src/harness/officialAgentLoopHost.ts"

# 匹配「把可能缺失的值兜底成常量」的形态：
#   typeof args.X === 'string' ? args.X : 'Y'
#   args.X ?? 'Y'   /   args.X || 'Y'
COERCE_TYPEOF = re.compile(
    r"""typeof\s+args\.(?P<field>\w+)\s*===\s*'(?P<type>[\w\[\]]+)'\s*\?\s*args\.(?P=field)\s*:\s*(?P<default>''|'[^']*'|\[\]|\{\})"""
)
COERCE_NULLISH = re.compile(
    r"""args\.(?P<field>\w+)\s*(?:\?\?|\|\|)\s*(?P<default>''|'[^']*'|\[\]|\{\})"""
)


def parse_tools(text: str) -> list[dict]:
    """从注册调用里抽出每个工具的名字、parameters 块、buildArguments 块。"""
    tools: list[dict] = []
    # 以 `name: 'x',` 为工具起点，截到下一个 `registerBrokerTool(` 或文件尾
    starts = [m.start() for m in re.finditer(r"name:\s*'(read_file|write_file|run_tests|git_diff|seal_candidate|[\w-]+)'", text)]
    starts.append(len(text))
    for i in range(len(starts) - 1):
        seg = text[starts[i] : starts[i + 1]]
        m_name = re.search(r"name:\s*'([\w-]+)'", seg)
        if not m_name:
            continue
        name = m_name.group(1)
        m_params = re.search(r"parameters:\s*\{", seg)
        if not m_params:
            continue
        # 花括号配对，取 parameters 块
        depth = 0
        start = m_params.end() - 1
        for j in range(start, len(seg)):
            if seg[j] == "{":
                depth += 1
            elif seg[j] == "}":
                depth -= 1
                if depth == 0:
                    params_block = seg[start : j + 1]
                    break
        else:
            continue
        m_ba = re.search(r"buildArguments:\s*\(", seg)
        ba_block = seg[m_ba.start() :] if m_ba else ""
        line_no = text[: starts[i]].count("\n") + 1
        tools.append(
            {
                "name": name,
                "line": line_no,
                "params_block": params_block,
                "ba_block": ba_block,
            }
        )
    return tools


def declared_required(params_block: str) -> set[str]:
    """取参数块里标了 required: true 的字段名。"""
    out: set[str] = set()
    for m in re.finditer(
        r"(?P<field>\w+)\s*:\s*\{[^{}]*required:\s*true[^{}]*\}", params_block, re.DOTALL
    ):
        out.add(m.group("field"))
    return out


def declared_fields(params_block: str) -> set[str]:
    """取参数块里声明的字段名（顶层标识符: {）。"""
    inner = params_block[1:-1]  # 去外层花括号
    return set(re.findall(r"(?:^|[\s,{])(?P<field>\w+)\s*:\s*\{", inner))


def coerced_fields(ba_block: str) -> dict[str, str]:
    """取被兜底的字段名 → 默认值字面量。"""
    out: dict[str, str] = {}
    for m in COERCE_TYPEOF.finditer(ba_block):
        out[m.group("field")] = m.group("default")
    for m in COERCE_NULLISH.finditer(ba_block):
        out.setdefault(m.group("field"), m.group("default"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="检查工具必填声明与静默填空是否一致")
    ap.add_argument("--list", action="store_true", help="只列出工具与字段，不判定")
    ap.add_argument(
        "--file", default=str(TARGET), help=f"被测文件（默认 {TARGET.relative_to(REPO)}）"
    )
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"失败关闭：找不到被测文件 {path}", file=sys.stderr)
        return 2

    text = path.read_text(encoding="utf-8")
    tools = parse_tools(text)
    if not tools:
        print("失败关闭：未从文件中解析出任何工具注册（解析器可能已过期）", file=sys.stderr)
        return 2

    violations: list[str] = []
    placeholder_defaults: list[str] = []

    print(f"被测文件：{path.relative_to(REPO) if path.is_relative_to(REPO) else path}")
    print()
    for t in tools:
        fields = declared_fields(t["params_block"])
        req = declared_required(t["params_block"])
        coerced = coerced_fields(t["ba_block"])
        print(f"  工具 {t['name']}（行 {t['line']}）")
        print(f"    声明字段 : {sorted(fields) or '(无)'}")
        print(f"    标为必填 : {sorted(req) or '(无)'}")
        print(f"    兜底字段 : {coerced or '(无)'}")

        # 判据：被兜底成「空串/空数组」的字段必须标 required: true
        # （兜底成**合法值**如 'public' 不视为违规 —— 那是真默认，不丢信息）
        for field, default in coerced.items():
            if default in ("''", "[]", "{}"):
                placeholder_defaults.append(f"    · {t['name']}.{field} → {default}")
                if field not in req:
                    violations.append(
                        f"    ❌ {t['name']}.{field}：被兜底成 {default}（静默填空），"
                        f"但 parameters 里未标 required: true ⇒ 模型收到的 schema 说它是可选的"
                    )
        print()

    if args.list:
        return 0

    print("=" * 62)
    if placeholder_defaults:
        print("发现「静默填空」的字段（把缺失变成空值，抹掉自纠所需信息）：")
        for line in placeholder_defaults:
            print(line)
    else:
        print("未发现「兜底成空值」的字段。")

    print()
    if violations:
        print("必填声明不一致：")
        for line in violations:
            print(line)
        print()
        print(f"❌ 共 {len(violations)} 处不一致。")
        print("   修法：① 给上述字段补 `required: true`（上游据此生成 JSON Schema 的 required[]）；")
        print("        ② 或改为产出**指名字段**的错误（如 MISSING_<FIELD>），不再静默填空。")
        print("   依据：Issue #68/#70 的失败链前两环；实测占失败 ~43% 的硬停由此触发。")
        return 1

    print(f"✅ 一致：{len(tools)} 个工具，兜底字段均已标为必填。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
