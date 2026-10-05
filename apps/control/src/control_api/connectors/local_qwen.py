"""本地 omlx/Qwen OpenAI 兼容连接器；缺 API key 失败关闭。

本机 MLX/大模型首 token 可能很慢；默认读超时 180s，可用
``RING_LOCAL_QWEN_TIMEOUT``（秒）覆盖。过短会导致 Temporal
RunActivation 失败后无限重试，联调看似「卡住」。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse


class LocalQwenUnavailable(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def default_chat_timeout() -> float:
    """本机 Qwen 读超时（秒）。"""
    raw = (os.environ.get("RING_LOCAL_QWEN_TIMEOUT") or "").strip()
    if raw:
        try:
            return max(30.0, float(raw))
        except ValueError:
            pass
    return 180.0


def is_loopback_inference_base(base_url: str) -> bool:
    """仅 loopback 主机视为本机推理（可计 gpu_seconds）；远程 API 不计本机 GPU。"""
    host = (urlparse(base_url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def format_gpu_seconds(elapsed_wall_seconds: float) -> str | None:
    """墙钟秒 → DecimalString；≤0 返回 None（无可得用量）。"""
    if elapsed_wall_seconds <= 0:
        return None
    text_v = f"{elapsed_wall_seconds:.3f}".rstrip("0").rstrip(".")
    return text_v or None


def chat_completion(
    *,
    base_url: str,
    api_key: str,
    model_id: str,
    max_tokens: int,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """OpenAI 兼容 chat/completions。

    ``tools`` 非空时模型可返回 ``tool_calls``（Harness 侧循环用）；
    Control ``dispatch`` 默认路径仍可不传 tools。
    返回含 ``elapsed_wall_seconds``（调用墙钟），供本机 GPU 计量。
    """
    if not api_key:
        raise LocalQwenUnavailable("本地模型 API key 未配置")
    base = base_url.rstrip("/")
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": messages
        or [{"role": "user", "content": "ping; reply with the single token pong"}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    request = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=raw,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    wait = default_chat_timeout() if timeout is None else max(30.0, float(timeout))
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=wait) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise LocalQwenUnavailable(f"模型调用失败 HTTP {exc.code}: {detail}") from exc
    except TimeoutError as exc:
        raise LocalQwenUnavailable(
            f"模型读超时（{wait:.0f}s）；可调大 RING_LOCAL_QWEN_TIMEOUT"
        ) from exc
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        raise LocalQwenUnavailable(f"模型调用失败: {exc}") from exc
    elapsed = max(0.0, time.monotonic() - started)
    return {
        "request_bytes": raw,
        "response": body,
        "timeout_seconds": wait,
        "elapsed_wall_seconds": elapsed,
    }


def list_models(*, base_url: str, api_key: str, timeout: float = 10.0) -> dict[str, Any]:
    if not api_key:
        raise LocalQwenUnavailable("本地模型 API key 未配置")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except Exception as exc:
        raise LocalQwenUnavailable(f"列举模型失败: {exc}") from exc
