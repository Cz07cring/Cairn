"""写入 .runtime/chat.env：把 OpenAI 兼容 chat 后端切到 DeepSeek（开发期）。

用法：
  export DEEPSEEK_API_KEY=sk-...
  uv run python scripts/use_deepseek_chat.py

恢复本机 Qwen：删除 .runtime/chat.env 后重启 serve_local / test_local。
密钥只写进 gitignore 的 .runtime/，禁止提交。

注意：本脚本会写入 RING_CLOUD_MODE=PREAUTHORIZED（部署层允许云）。
Goal/Probe 用的 ModelProfile 仍须 cloud_mode=PREAUTHORIZED 且
cloud_provider_refs 含 RING_CHAT_CLOUD_PROVIDER_REF，dispatch 才会外呼。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_TIMEOUT = "120"
DEFAULT_PROVIDER_REF = "deepseek:api"


def _atomic_write_0600(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        print(
            "缺少 DEEPSEEK_API_KEY。请先 export 后再运行，"
            "或手动创建 .runtime/chat.env（见 deploy/local/chat.env.example）。",
            file=sys.stderr,
        )
        return 1
    base = (os.environ.get("DEEPSEEK_API_BASE") or DEFAULT_BASE).rstrip("/")
    model = (os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL).strip()
    timeout = (os.environ.get("DEEPSEEK_TIMEOUT") or DEFAULT_TIMEOUT).strip()
    provider_ref = (
        os.environ.get("RING_CHAT_CLOUD_PROVIDER_REF") or DEFAULT_PROVIDER_REF
    ).strip()

    path = root / ".runtime" / "chat.env"
    body = "\n".join(
        [
            "# 由 scripts/use_deepseek_chat.py 生成；勿提交。",
            "# 存在本文件时，serve_local / test_local / live 测覆盖本机 Qwen（:8001 留给 OpenCode）。",
            "# 连接器指向 ≠ 合同授权：ModelProfile 仍须 PREAUTHORIZED + cloud_provider_refs。",
            "RING_CHAT_PROVIDER=deepseek",
            "RING_CHAT_NOTE=dev-override-opencode-owns-local-qwen",
            f"RING_CHAT_CLOUD_PROVIDER_REF={provider_ref}",
            "RING_CLOUD_MODE=PREAUTHORIZED",
            f"RING_LOCAL_QWEN_BASE={base}",
            f"RING_LOCAL_QWEN_API_KEY={key}",
            f"RING_LOCAL_QWEN_MODEL={model}",
            f"RING_LOCAL_QWEN_TIMEOUT={timeout}",
            "",
        ]
    )
    _atomic_write_0600(path, body)
    mode = oct(path.stat().st_mode & 0o777)
    print(
        f"已写入 {path}（provider=deepseek model={model} base={base} mode={mode}）。"
        "请重启 serve_local；自检：uv run python scripts/probe_local_qwen.py"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
