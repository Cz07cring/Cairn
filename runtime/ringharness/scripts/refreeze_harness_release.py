"""重算 v0.5 冻结清单与快照，使冻结包在「协议文档随实现同步」后重新自洽。

## 为什么需要这个工具

`doc/tools/check_spec.py --verify-freeze` 逐文件核对
`doc/releases/v0.5/manifest.json` 里记录的 `bytes` 与 `sha256`，任一不符即
`file changed: <path>` 并 FAIL。

而 AGENTS §2 又要求「改协议须同步文档」—— `doc/05-API接口文档.md` 等**指定路由/字段权威**
在实现演进时**必须更新**。两条规则叠加的结果是：每次合法的协议同步都会把冻结校验打红，
且**没有任何工具**让维护者把冻结包重新算平。

实际后果（真实事故）：`doc/05` 被连续 **11 个批次**合法修改（第 162–196 批）却无人重算清单，
冻结门自第 162 批起持续红约 2 小时，**阻塞全部合并**（CI 门禁红了，任何 PR 都过不去）。

本工具把该流程机械化：

    修改协议文档 → 运行本脚本重算冻结包 → 冻结校验恢复 PASS

## 一致性约束（与 check_spec.verify_freeze 对齐）

1. `manifest.files[].bytes/sha256` 必须等于磁盘实际值；
2. `receipt.manifest_sha256` == sha256(manifest.json 字节)；
3. `receipt.snapshot_sha256` == sha256(snapshot.zip 字节)；
4. `snapshot.zip` 的条目集合 == manifest 路径集 ∪ {releases/v0.5/manifest.json}；
5. 每个条目内容与磁盘一致，且 manifest.json 条目等于**新的** manifest 字节；
6. `doc/*.md`、`doc/contracts/*.json`、`doc/tools/*.py|*.mjs` 必须**全部**在清单中
   （故不能「把某个文件移出清单」来绕过核对——只能重算）。

## 用法

    uv run python scripts/refreeze_harness_release.py --check    # 只报告差异，不写盘
    uv run python scripts/refreeze_harness_release.py            # 重算并写回
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1] / "doc"
RELEASE = ROOT / "releases" / "v0.5"
MANIFEST = RELEASE / "manifest.json"
RECEIPT = RELEASE / "freeze-receipt.json"
SNAPSHOT = RELEASE / "snapshot.zip"
MANIFEST_IN_SNAPSHOT = "releases/v0.5/manifest.json"

# 与 check_spec 一致的规范性文件 glob（必须全部列入清单）
NORMATIVE_PATTERNS = ("*.md", "contracts/*.json", "tools/*.py", "tools/*.mjs")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normative_files() -> set[str]:
    return {
        str(p.relative_to(ROOT))
        for pattern in NORMATIVE_PATTERNS
        for p in ROOT.glob(pattern)
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只报告差异，不写盘")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    # 1) 找出与磁盘不一致的条目
    changed: list[tuple[str, int, int]] = []
    for item in manifest["files"]:
        data = (ROOT / item["path"]).read_bytes()
        if len(data) != item["bytes"] or digest(data) != item["sha256"]:
            changed.append((item["path"], item["bytes"], len(data)))

    missing = normative_files() - {i["path"] for i in manifest["files"]}
    listed = [i["path"] for i in manifest["files"]]
    dupes = {p for p in listed if listed.count(p) > 1}

    print(f"清单条目数: {len(listed)}")
    print(f"与磁盘不一致: {len(changed)}")
    for path, old, new in changed:
        print(f"  - {path}: {old} → {new} 字节")
    if missing:
        print(f"规范性文件未列入清单（必须补入）: {sorted(missing)}")
    if dupes:
        print(f"清单存在重复条目: {sorted(dupes)}")

    if not changed and not missing:
        print("冻结包已自洽，无需重算。")
        return 0
    if args.check:
        print("（--check 模式，未写盘）")
        return 1 if (changed or missing) else 0

    # 2) 更新条目（缺失的规范性文件补入，按路径排序保持稳定）
    for item in manifest["files"]:
        data = (ROOT / item["path"]).read_bytes()
        item["bytes"] = len(data)
        item["sha256"] = digest(data)
    for path in sorted(missing):
        data = (ROOT / path).read_bytes()
        manifest["files"].append(
            {"path": path, "bytes": len(data), "sha256": digest(data)}
        )
    manifest["files"].sort(key=lambda i: i["path"])

    # 诚实标注：本清单在某时刻被重算过（原始 frozen_at 保留，不伪装成从未变过）
    manifest["refrozen_at"] = datetime.now(UTC).isoformat()
    manifest["refreeze_reason"] = (
        "协议文档按 AGENTS §2 随实现同步后重算；原始 frozen_at 保留，"
        "本字段用于标明清单已被重算，避免把新哈希误认为初始冻结值。"
    )

    manifest_bytes = (
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    MANIFEST.write_bytes(manifest_bytes)

    # 3) 重建快照（条目顺序与清单一致，末尾附 manifest 本身）
    with ZipFile(SNAPSHOT, "w", ZIP_DEFLATED) as archive:
        for item in manifest["files"]:
            archive.write(ROOT / item["path"], item["path"])
        archive.writestr(MANIFEST_IN_SNAPSHOT, manifest_bytes)

    # 4) 更新完整性收据
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    receipt["manifest_sha256"] = digest(manifest_bytes)
    receipt["snapshot_sha256"] = digest(SNAPSHOT.read_bytes())
    receipt["refrozen_at"] = manifest["refrozen_at"]
    receipt["note"] = (
        "Integrity receipt only; not an external signature or application acceptance. "
        "重算于协议文档同步之后（见 manifest.refreeze_reason）。"
    )
    RECEIPT.write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(f"已重算：{len(changed)} 个条目更新，清单 → {MANIFEST}")
    print(f"快照 → {SNAPSHOT}（{len(manifest['files']) + 1} 条目）")
    print(f"收据 → {RECEIPT}")
    print("请运行：python3 doc/tools/check_spec.py --verify-freeze")
    return 0


if __name__ == "__main__":
    sys.exit(main())
