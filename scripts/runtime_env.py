"""合并 .runtime 模型环境：qwen.env 为默认，chat.env 可覆盖文件默认（DeepSeek 等）。

优先级（Codex P0）：进程显式 export > chat.env > qwen.env。
chat.env 不得压过调用者已设置的 RING_LOCAL_QWEN_* / RING_CLOUD_MODE。
不把密钥写入仓库。chat.env 存在时，ringharness live/probe 不再抢本机 :8001
（留给 OpenCode）；恢复本机 Qwen 时删除或改名 chat.env 即可。

``.runtime/chat.env`` 是开发连接器指向，不等于合同授权：远程端点仍须
ModelProfile.cloud_mode 与部署 RING_CLOUD_MODE 允许后才可外呼。
"""

from __future__ import annotations

from pathlib import Path

# chat.env 允许写入的键（OpenAI 兼容 chat 后端；历史名仍带 QWEN）
_CHAT_OVERRIDE_KEYS = frozenset(
    {
        "RING_LOCAL_QWEN_BASE",
        "RING_LOCAL_QWEN_API_KEY",
        "RING_LOCAL_QWEN_MODEL",
        "RING_LOCAL_QWEN_TIMEOUT",
        "RING_CHAT_PROVIDER",
        "RING_CHAT_NOTE",
        "RING_CLOUD_MODE",
        "RING_CHAT_CLOUD_PROVIDER_REF",
    }
)


def merge_dotenv_setdefault(path: Path, env: dict[str, str]) -> None:
    """已存在的键不覆盖（外层显式 export / 先前文件优先）。"""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env.setdefault(key.strip(), value.strip())


def merge_dotenv_fill_missing(
    path: Path, env: dict[str, str], *, keys: frozenset[str], protected: set[str]
) -> None:
    """仅填充 protected 中尚未出现的指定键（不覆盖进程显式环境）。"""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key not in keys:
            continue
        if key in protected:
            continue
        env[key] = value.strip()


def apply_model_runtime_env(root: Path, env: dict[str, str]) -> None:
    """qwen.env → setdefault；chat.env → 覆盖文件默认，但不压进程显式键。"""
    protected = set(env.keys())
    merge_dotenv_setdefault(root / ".runtime" / "qwen.env", env)
    # chat 覆盖 qwen 的默认，但跳过启动时已在进程环境中的键
    merge_dotenv_fill_missing(
        root / ".runtime" / "chat.env",
        env,
        keys=_CHAT_OVERRIDE_KEYS,
        protected=protected,
    )


def chat_provider_label(env: dict[str, str] | None = None) -> str:
    """可读标签，供探针/日志；不读密钥。"""
    src = env if env is not None else {}
    explicit = (src.get("RING_CHAT_PROVIDER") or "").strip()
    if explicit:
        return explicit
    base = (src.get("RING_LOCAL_QWEN_BASE") or "").lower()
    if "deepseek.com" in base:
        return "deepseek"
    if "127.0.0.1:8001" in base or "localhost:8001" in base:
        return "local-qwen"
    if base:
        return "openai-compatible"
    return "unset"
