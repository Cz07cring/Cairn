"""chat 模型 id 与 /v1/models 目录对齐闸门（失败关闭）。

``.runtime/chat.env`` 可覆盖连接器，但进程显式 ``RING_LOCAL_QWEN_BASE``
常与 chat 的 MODEL 错配（如 :8001 + deepseek-flash）。列表非空且配置
model 不在其中时硬失败，禁止 live/probe 带着「model not found」空转。

瞬时 SSL/连接失败会短暂重试；耗尽后以 ``CHAT_CATALOG_UNREACHABLE`` 失败关闭
（≠ 配置错配 ``CHAT_MODEL_MISMATCH``；rc-ds10 末两轮 SSL EOF 实证）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

# 瞬时网络：重试次数含首次；间隔秒（rc-ds10：末两轮 SSL EOF 1s 假红）
_FETCH_ATTEMPTS = 3
_FETCH_BACKOFF_S = (0.4, 1.0)


class ChatModelMismatch(Exception):
    """配置的 model_id 不在端点目录中，或目录不可达。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ChatModelGateResult:
    ok: bool
    base: str
    model: str
    model_ids: list[str]
    error: str | None = None

    @property
    def mismatch(self) -> bool:
        return bool(self.model) and bool(self.model_ids) and self.model not in self.model_ids


def model_id_in_catalog(model: str, model_ids: list[str]) -> bool:
    """精确匹配；别名必须出现在目录里，否则 fail-closed。"""
    mid = (model or "").strip()
    if not mid:
        return False
    return mid in model_ids


def parse_model_ids(payload: dict) -> list[str]:
    ids: list[str] = []
    for item in payload.get("data") or []:
        if isinstance(item, dict):
            mid = item.get("id")
            if isinstance(mid, str) and mid.strip():
                ids.append(mid.strip())
    return ids


def _is_transient_catalog_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    needles = (
        "unexpected_eof",
        "ssl",
        "timed out",
        "timeout",
        "connection reset",
        "connection refused",
        "temporarily unavailable",
        "broken pipe",
        "eof occurred",
    )
    return any(n in text for n in needles)


def fetch_model_ids(
    *,
    base_url: str,
    api_key: str,
    timeout: float = 15.0,
    attempts: int = _FETCH_ATTEMPTS,
) -> list[str]:
    if not api_key.strip():
        raise ChatModelMismatch("RING_LOCAL_QWEN_API_KEY 未设置，无法核对模型目录")
    base = base_url.rstrip("/")
    request = urllib.request.Request(
        f"{base}/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    last_exc: BaseException | None = None
    tries = max(1, int(attempts))
    for i in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode())
            if not isinstance(body, dict):
                raise ChatModelMismatch("列举模型失败: 响应非 JSON object")
            return parse_model_ids(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            # 5xx 可重试；4xx 多为鉴权/路径，不重试
            if exc.code >= 500 and i + 1 < tries:
                last_exc = exc
                time.sleep(_FETCH_BACKOFF_S[min(i, len(_FETCH_BACKOFF_S) - 1)])
                continue
            raise ChatModelMismatch(
                f"列举模型失败 HTTP {exc.code}: {detail}"
            ) from exc
        except ChatModelMismatch:
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_exc = exc
            if i + 1 < tries and _is_transient_catalog_error(exc):
                time.sleep(_FETCH_BACKOFF_S[min(i, len(_FETCH_BACKOFF_S) - 1)])
                continue
            if _is_transient_catalog_error(exc):
                raise ChatModelMismatch(
                    f"CHAT_CATALOG_UNREACHABLE: 列举模型失败（已重试 {tries} 次）: {exc}"
                ) from exc
            raise ChatModelMismatch(f"列举模型失败: {exc}") from exc
    raise ChatModelMismatch(
        f"CHAT_CATALOG_UNREACHABLE: 列举模型失败（已重试 {tries} 次）: {last_exc}"
    )


def evaluate_chat_model_gate(
    *,
    base_url: str,
    api_key: str,
    model: str,
    model_ids: list[str] | None = None,
    timeout: float = 15.0,
) -> ChatModelGateResult:
    """核对配置 model 是否在目录中。

    ``model_ids`` 非 None 时不发网络（单测）；为空列表视为「目录不可用」不判 mismatch。
    """
    base = (base_url or "").rstrip("/")
    mid = (model or "").strip()
    ids = list(model_ids) if model_ids is not None else []
    err: str | None = None
    if model_ids is None:
        try:
            ids = fetch_model_ids(base_url=base, api_key=api_key, timeout=timeout)
        except ChatModelMismatch as exc:
            return ChatModelGateResult(
                ok=False, base=base, model=mid, model_ids=[], error=exc.message
            )
    if not mid:
        return ChatModelGateResult(
            ok=False,
            base=base,
            model=mid,
            model_ids=ids,
            error="RING_LOCAL_QWEN_MODEL 未设置",
        )
    if not ids:
        return ChatModelGateResult(
            ok=False,
            base=base,
            model=mid,
            model_ids=ids,
            error=err or "模型目录为空，无法核对 RING_LOCAL_QWEN_MODEL",
        )
    if mid not in ids:
        hint = (
            "请修正 .runtime/chat.env 与进程 RING_LOCAL_QWEN_* 对齐"
            "（常见错配：本机 :8001 配了 deepseek-*；或删 chat.env 恢复本机 Qwen）。"
            f" 可用: {', '.join(ids[:12])}{'…' if len(ids) > 12 else ''}"
        )
        return ChatModelGateResult(
            ok=False,
            base=base,
            model=mid,
            model_ids=ids,
            error=f"CHAT_MODEL_MISMATCH: RING_LOCAL_QWEN_MODEL={mid} 不在 {base}/v1/models。{hint}",
        )
    return ChatModelGateResult(ok=True, base=base, model=mid, model_ids=ids)


def assert_chat_model_configured(env: dict[str, str], *, timeout: float = 15.0) -> ChatModelGateResult:
    """从合并后的 env 失败关闭；ok 则返回结果。"""
    result = evaluate_chat_model_gate(
        base_url=(env.get("RING_LOCAL_QWEN_BASE") or "").strip(),
        api_key=(env.get("RING_LOCAL_QWEN_API_KEY") or "").strip(),
        model=(env.get("RING_LOCAL_QWEN_MODEL") or "").strip(),
        timeout=timeout,
    )
    if not result.ok:
        raise ChatModelMismatch(result.error or "CHAT_MODEL_MISMATCH")
    return result
