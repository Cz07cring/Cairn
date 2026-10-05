"""联调栈启动器：四类「静默降级」必须失败关闭（≠DONE）。

每个用例对应 2026-09-15 实测踩到的一个坑；此处只锁**接线**，不重测底层语义
（模型目录闸门的解析/匹配由 test_chat_model_gate.py 覆盖）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import chat_model_gate as gate
import serve_joint_stack as stack


def test_model_gate_fails_closed_on_mismatch(monkeypatch):
    """模型不在端点目录 → 拒绝起栈。

    这是「跑不起来」的总根：错配时若照常起栈，系统会静默空转成
    NO_TOOL_PROPOSAL，Goal 永久停在 RUNNING。宁可起不来。
    """
    monkeypatch.setattr(
        gate,
        "evaluate_chat_model_gate",
        lambda **_: gate.ChatModelGateResult(
            ok=False,
            base="https://api.deepseek.com",
            model="Qwen3.8-Flash-Next-Uncensored-Mixed-omlx",
            model_ids=[],
            error="CHAT_MODEL_MISMATCH: 错配",
        ),
    )
    env = {
        "RING_LOCAL_QWEN_BASE": "https://api.deepseek.com",
        "RING_LOCAL_QWEN_API_KEY": "k",
        "RING_LOCAL_QWEN_MODEL": "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx",
    }
    with pytest.raises(SystemExit) as exc:
        stack.assert_model_aligned(env)
    assert exc.value.code == 2


def test_model_gate_passes_when_aligned(monkeypatch):
    monkeypatch.setattr(
        gate,
        "evaluate_chat_model_gate",
        lambda **_: gate.ChatModelGateResult(
            ok=True,
            base="https://api.deepseek.com",
            model="deepseek-flash",
            model_ids=["deepseek-flash"],
        ),
    )
    env = {
        "RING_LOCAL_QWEN_BASE": "https://api.deepseek.com",
        "RING_LOCAL_QWEN_API_KEY": "k",
        "RING_LOCAL_QWEN_MODEL": "deepseek-flash",
    }
    stack.assert_model_aligned(env)  # 不抛即通过


def test_runner_env_carries_live_switches():
    """runner 必须带 live 派发与官方 AgentLoop 开关。

    缺 LIVE_DISPATCH ⇒ PLAN 走夹具分支（正文 `fixture-plan-host`，约 128ms），
    报 LivePlanParseError，表现为「模型调通了却解析不出 JSON」。
    """
    out = stack._runner_env({"A": "1"}, "jwt-token")
    assert out["RING_RUNNER_LIVE_DISPATCH"] == "1"
    assert out["RING_HARNESS_EXECUTE_RUNTIME"] == "deepseek-official-agent-loop"
    assert out["RING_RUNNER_WORKER_JWT"] == "jwt-token"
    assert out["RING_WORKER_JWT"] == "jwt-token"
    assert out["A"] == "1"


def test_broker_env_drops_business_db_credentials():
    """broker 不得持业务库凭据（AGENTS 依赖边界），但须拿到 broker 三件套。"""
    out = stack._broker_env(
        {
            "RING_DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:5/ring_test",
            "RING_TEST_DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:5/ring_test",
            "RING_CONTROL_DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:5/ring_test",
            "RING_LOCAL_QWEN_MODEL": "deepseek-flash",
        },
        "jwt-token",
    )
    assert not [k for k in out if k.startswith("RING_DATABASE")]
    assert "RING_TEST_DATABASE_URL" not in out
    assert "RING_CONTROL_DATABASE_URL" not in out
    assert out["RING_BROKER_WORKER_JWT"] == "jwt-token"
    assert out["RING_BROKER_CONTROL_URL"] == stack.CONTROL_URL
    assert out["RING_LOCAL_QWEN_MODEL"] == "deepseek-flash"



