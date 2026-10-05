"""Broker 长驻进程：非调度权威。

边界（见 AGENTS.md / 工程目录补充）：
- 不得持有业务库 Goal/Task 写凭据；副作用只经 Control HTTP（worker JWT）。
- 不得将 Goal/Task 标为 DONE（完成判定仅属 ControlKernel + VerificationProfile）。
- 不得另立调度权威：本进程不轮询/不推进 Goal 或 Task。
- 有 RING_BROKER_CONTROL_URL + worker JWT 时轮询
  GET /internal/v1/broker/effects，仅执行已 PREPARED/AUTHORIZED 且本租约持有的 effect。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from .host import (
    run_git_diff_effect,
    run_read_file_effect,
    run_run_tests_effect,
    run_seal_candidate_effect,
    run_write_file_effect,
)

logger = logging.getLogger("execution_broker")

# 控制面业务库写凭据；出现在 Broker 环境即失败关闭。
_FORBIDDEN_DB_ENV = (
    "RING_DATABASE_URL",
    "RING_CONTROL_DATABASE_URL",
)


@dataclass(frozen=True)
class BrokerSettings:
    """运行时配置；control_url 缺省表示无法调用 effect API，只能空闲。"""

    control_url: str | None
    worker_jwt: str | None
    poll_interval_seconds: float = 5.0
    workspace_root: str | None = None
    allowed_paths: tuple[str, ...] = field(default_factory=tuple)


def refuse_business_db_credentials(env: Mapping[str, str]) -> None:
    """Broker 不得直连业务库写 Goal/Task；检测到库 URL 即拒绝启动。"""
    present = [key for key in _FORBIDDEN_DB_ENV if (env.get(key) or "").strip()]
    if present:
        joined = " ".join(present)
        raise RuntimeError(
            f"Broker 不得持有业务库写凭据（检测到 {joined}）；"
            "仅可通过 RING_BROKER_CONTROL_URL + worker JWT 调用 effect / StopReceipt API"
        )


def load_settings(environ: Mapping[str, str] | None = None) -> BrokerSettings:
    env = os.environ if environ is None else environ
    refuse_business_db_credentials(env)
    control_url = (env.get("RING_BROKER_CONTROL_URL") or "").strip() or None
    worker_jwt = (env.get("RING_BROKER_WORKER_JWT") or "").strip() or None
    workspace_root = (env.get("RING_BROKER_WORKSPACE_ROOT") or "").strip() or None
    allowed_raw = (env.get("RING_BROKER_ALLOWED_PATHS") or "").strip()
    if allowed_raw:
        allowed_paths = tuple(p.strip() for p in allowed_raw.split(",") if p.strip())
    elif workspace_root:
        allowed_paths = ("src/**",)
    else:
        allowed_paths = ()
    return BrokerSettings(
        control_url=control_url,
        worker_jwt=worker_jwt,
        workspace_root=workspace_root,
        allowed_paths=allowed_paths,
    )


def refuse_mark_goal_done(*_args, **_kwargs) -> None:
    """显式拒绝：Broker / 模型成功 / shell exit 0 都不能写成 Goal DONE。"""
    raise PermissionError(
        "Broker 不得将 Goal 标为 DONE；完成判定仅属 ControlKernel + VerificationProfile"
    )


def health() -> dict:
    """进程健康视图；声明非调度器、不写 Goal DONE。"""
    return {
        "status": "ok",
        "role": "execution-broker",
        "scheduler": False,
        "marks_goal_done": False,
    }


def _join_url(base_url: str | None, path: str) -> str:
    if not base_url:
        return path
    return f"{base_url.rstrip('/')}{path}"


def _worker_auth_headers(worker_jwt: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {worker_jwt}"}


def poll_dispatchable(
    client: Any,
    *,
    base_url: str | None,
    worker_auth: Mapping[str, str],
    limit: int = 32,
) -> list[dict]:
    """GET /internal/v1/broker/effects；返回 data 列表（dict）。不轮询 Goal/Task。"""
    url = _join_url(base_url, "/internal/v1/broker/effects")
    response = client.get(url, params={"limit": limit}, headers=dict(worker_auth))
    if response.status_code != 200:
        raise RuntimeError(
            f"拉取可派发 effect 失败：HTTP {response.status_code} {getattr(response, 'text', '')}"
        )
    body = response.json()
    data = body.get("data") if isinstance(body, dict) else None
    if data is None:
        return []
    if not isinstance(data, list):
        raise TypeError("拉取可派发 effect 响应格式无效")
    return data


def process_dispatchable_once(
    client: Any,
    *,
    base_url: str | None,
    worker_auth: Mapping[str, str],
    settings: BrokerSettings,
    run_read_file: Callable[..., dict] = run_read_file_effect,
    run_write_file: Callable[..., dict] = run_write_file_effect,
    run_run_tests: Callable[..., dict] = run_run_tests_effect,
    run_git_diff: Callable[..., dict] = run_git_diff_effect,
    run_seal_candidate: Callable[..., dict] = run_seal_candidate_effect,
    limit: int = 32,
) -> list[dict]:
    """一轮 poll：登记工具在配置了 workspace 时真实执行。"""
    items = poll_dispatchable(
        client, base_url=base_url, worker_auth=worker_auth, limit=limit
    )
    if not settings.workspace_root:
        logger.info("broker-effects-pending count=%s", len(items))
        return items

    workspace = Path(settings.workspace_root)
    allowed = list(settings.allowed_paths) or ["src/**"]
    supported = {"read_file", "write_file", "run_tests", "git_diff", "seal_candidate"}
    for item in items:
        effect = item.get("effect") if isinstance(item, dict) else None
        lease = item.get("lease") if isinstance(item, dict) else None
        if not isinstance(effect, dict) or not isinstance(lease, dict):
            logger.warning("broker-effects-skip malformed item")
            continue
        tool_ref = effect.get("tool_ref")
        if tool_ref not in supported:
            logger.info("broker-effects-skip unsupported tool_ref=%s", tool_ref)
            continue
        input_id = effect.get("input_artifact_id")
        if not input_id:
            logger.warning("broker-effects-skip missing input_artifact_id effect=%s", effect.get("id"))
            continue
        content_url = _join_url(base_url, f"/api/v1/artifacts/{input_id}/content")
        content_resp = client.get(content_url, headers=dict(worker_auth))
        if content_resp.status_code != 200:
            logger.error(
                "broker-effects-input-download-failed effect=%s status=%s",
                effect.get("id"),
                content_resp.status_code,
            )
            continue
        input_bytes = content_resp.content
        project_id = UUID(str(effect["project_id"]))
        lease_body = {
            "activity_id": lease["activity_id"],
            "attempt_id": lease["attempt_id"],
            "fencing_epoch": str(lease["fencing_epoch"]),
        }
        common = {
            "client": client,
            "worker_auth": dict(worker_auth),
            "lease": lease_body,
            "effect": effect,
            "project_id": project_id,
            "workspace_root": workspace,
            "input_bytes": input_bytes,
        }
        if tool_ref == "read_file":
            run_read_file(**common, allowed_paths=allowed)
        elif tool_ref == "write_file":
            run_write_file(**common, allowed_paths=allowed)
        elif tool_ref == "run_tests":
            run_run_tests(**common)
        elif tool_ref == "git_diff":
            run_git_diff(**common)
        else:
            run_seal_candidate(**common, allowed_paths=allowed)
    return items


def run_loop(
    *,
    once: bool = False,
    settings: BrokerSettings | None = None,
    sleep=time.sleep,
    client: Any | None = None,
) -> int:
    """启动循环：有 control_url+jwt 则 poll effect 队列；从不轮询 Goal/Task 写路径。

    ``client`` 可注入 httpx.Client / TestClient；缺省在有 URL 时自建 httpx.Client。
    ``--once`` 供单测退出。
    """
    cfg = settings if settings is not None else load_settings()
    logger.info("broker health=%s", health())

    def _one_cycle(http_client: Any) -> None:
        logger.info(
            "broker control_url configured; polling effects (no Goal/Task poller; Kernel 为调度权威)"
        )
        auth = _worker_auth_headers(cfg.worker_jwt or "")
        process_dispatchable_once(
            http_client,
            base_url=cfg.control_url,
            worker_auth=auth,
            settings=cfg,
        )

    def _run_with_http(action: Callable[[Any], None]) -> None:
        if client is not None:
            action(client)
            return
        import httpx

        # base_url：host.run_read_file_effect 使用相对 path（与 TestClient 一致）。
        # trust_env=False：避免宿主机 SOCKS 代理在未装 socksio 时拖垮独立进程。
        with httpx.Client(
            base_url=(cfg.control_url or "").rstrip("/") or None,
            timeout=30.0,
            trust_env=False,
        ) as http_client:
            action(http_client)

    can_poll = bool(cfg.control_url and cfg.worker_jwt)

    if once:
        if can_poll:
            _run_with_http(_one_cycle)
        else:
            logger.info("broker-idle")
        return 0

    while True:
        if can_poll:
            _run_with_http(_one_cycle)
        else:
            logger.info("broker-idle")
        sleep(cfg.poll_interval_seconds)
