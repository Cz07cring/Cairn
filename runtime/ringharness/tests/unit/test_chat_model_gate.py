"""chat 模型目录闸门：错配失败关闭（≠DONE）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

from chat_model_gate import (
    ChatModelMismatch,
    assert_chat_model_configured,
    evaluate_chat_model_gate,
    model_id_in_catalog,
    parse_model_ids,
)


def test_parse_and_membership():
    ids = parse_model_ids(
        {"data": [{"id": "Qwen-A"}, {"id": "deepseek-flash"}, {"id": ""}]}
    )
    assert ids == ["Qwen-A", "deepseek-flash"]
    assert model_id_in_catalog("deepseek-flash", ids)
    assert not model_id_in_catalog("deepseek-v4-pro", ids)
    assert not model_id_in_catalog("", ids)


def test_loopback_base_rejects_foreign_model_id():
    """本机 :8001 配 deepseek-* 是常见错配，须硬失败。"""
    result = evaluate_chat_model_gate(
        base_url="http://127.0.0.1:8001",
        api_key="k",
        model="deepseek-flash",
        model_ids=["Qwen3.8-Flash-Next-Uncensored-Mixed-omlx"],
    )
    assert result.ok is False
    assert result.mismatch is True
    assert "CHAT_MODEL_MISMATCH" in (result.error or "")
    assert "8001" in (result.error or "")


def test_matching_model_passes():
    result = evaluate_chat_model_gate(
        base_url="https://api.deepseek.com",
        api_key="k",
        model="deepseek-flash",
        model_ids=["deepseek-flash", "deepseek-v4-pro"],
    )
    assert result.ok is True
    assert result.error is None


def test_empty_catalog_fails_closed():
    result = evaluate_chat_model_gate(
        base_url="http://127.0.0.1:8001",
        api_key="k",
        model="any",
        model_ids=[],
    )
    assert result.ok is False
    assert "目录为空" in (result.error or "")


def test_assert_raises_on_mismatch(monkeypatch):
    import chat_model_gate as gate

    monkeypatch.setattr(
        gate,
        "fetch_model_ids",
        lambda **_kwargs: ["local-only"],
    )
    with pytest.raises(ChatModelMismatch, match="CHAT_MODEL_MISMATCH"):
        assert_chat_model_configured(
            {
                "RING_LOCAL_QWEN_BASE": "http://127.0.0.1:8001",
                "RING_LOCAL_QWEN_API_KEY": "k",
                "RING_LOCAL_QWEN_MODEL": "deepseek-flash",
            }
        )


def test_assert_chat_model_configured_uses_env_keys(monkeypatch):
    """无网络：monkeypatch fetch_model_ids。"""
    import chat_model_gate as gate

    monkeypatch.setattr(
        gate,
        "fetch_model_ids",
        lambda **_kwargs: ["allowed-model"],
    )
    out = assert_chat_model_configured(
        {
            "RING_LOCAL_QWEN_BASE": "http://127.0.0.1:8001",
            "RING_LOCAL_QWEN_API_KEY": "k",
            "RING_LOCAL_QWEN_MODEL": "allowed-model",
        }
    )
    assert out.ok is True


def test_fetch_model_ids_retries_transient_ssl(monkeypatch):
    """瞬时 SSL EOF 重试后成功（rc-ds10 末两轮同族）。"""
    import urllib.error
    import urllib.request

    import chat_model_gate as gate

    calls = {"n": 0}

    class _Resp:
        def read(self) -> bytes:
            return b'{"data":[{"id":"deepseek-flash"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(_req, timeout=15.0):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError(
                Exception("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred")
            )
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gate.time, "sleep", lambda _s: None)
    ids = gate.fetch_model_ids(
        base_url="https://api.deepseek.com", api_key="k", attempts=3
    )
    assert ids == ["deepseek-flash"]
    assert calls["n"] == 3


def test_fetch_model_ids_exhaust_marks_unreachable(monkeypatch):
    import urllib.error
    import urllib.request

    import chat_model_gate as gate

    def always_fail(_req, timeout=15.0):
        raise urllib.error.URLError(
            Exception("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred")
        )

    monkeypatch.setattr(urllib.request, "urlopen", always_fail)
    monkeypatch.setattr(gate.time, "sleep", lambda _s: None)
    with pytest.raises(ChatModelMismatch, match="CHAT_CATALOG_UNREACHABLE"):
        gate.fetch_model_ids(
            base_url="https://api.deepseek.com", api_key="k", attempts=2
        )
