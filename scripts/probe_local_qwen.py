"""探测当前 chat 后端（本机 Qwen 或 .runtime/chat.env 覆盖的 DeepSeek 等）。

不冒充 Harness / PROBE_MODEL 已完成。优先合并 qwen.env + chat.env。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from chat_model_gate import evaluate_chat_model_gate
from runtime_env import apply_model_runtime_env, chat_provider_label

DEFAULT_BASE = "http://127.0.0.1:8001"


def _get_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 5.0) -> dict:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def main() -> int:
    env = {**os.environ}
    apply_model_runtime_env(ROOT, env)
    base = (env.get("RING_LOCAL_QWEN_BASE") or DEFAULT_BASE).rstrip("/")
    api_key = (env.get("RING_LOCAL_QWEN_API_KEY") or "").strip()
    model = (env.get("RING_LOCAL_QWEN_MODEL") or "").strip()
    provider = chat_provider_label(env)
    local = "127.0.0.1" in base or "localhost" in base

    report: dict = {
        "provider": provider,
        "base": base,
        "model": model or None,
        "note": "开发探针；不表示 RunActivation/Harness live 已完成。",
    }

    if local:
        health_url = f"{base}/health"
        try:
            body = _get_json(health_url, timeout=3)
            report["ok"] = body.get("status") == "healthy"
            report["url"] = health_url
            report["status"] = body.get("status")
            report["default_model"] = body.get("default_model")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(json.dumps({**report, "ok": False, "url": health_url, "error": str(exc)}, ensure_ascii=False))
            return 1
    else:
        # 云端 OpenAI 兼容面通常无 /health；以 /v1/models 为准
        report["ok"] = False
        report["url"] = f"{base}/v1/models"

    if api_key:
        models_url = f"{base}/v1/models"
        try:
            models = _get_json(
                models_url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
            ids = [
                item.get("id")
                for item in models.get("data") or []
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            ids = [mid.strip() for mid in ids if mid and mid.strip()]
            report["models_url"] = models_url
            report["model_ids"] = ids
            report["models_ok"] = True
            report["ok"] = True
            gate = evaluate_chat_model_gate(
                base_url=base,
                api_key=api_key,
                model=model,
                model_ids=ids,
            )
            if not gate.ok:
                # 失败关闭：不再用 model_warning 放行错配
                report["ok"] = False
                report["model_mismatch"] = True
                report["error"] = gate.error
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            report["models_ok"] = False
            report["models_error"] = f"HTTP {exc.code}: {detail}"
            report["ok"] = False
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            report["models_ok"] = False
            report["models_error"] = str(exc)
            report["ok"] = False
    else:
        report["models_ok"] = False
        report["models_error"] = "RING_LOCAL_QWEN_API_KEY 未设置"
        if not local:
            report["ok"] = False

    print(json.dumps(report, ensure_ascii=False))
    if report.get("model_mismatch"):
        return 3
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
