"""模型 chat 端点云/本地授权：按 ModelProfile.cloud_mode 失败关闭。

连接器 URL（含开发期 DeepSeek 覆盖）不等于合同授权。DENY 只允许 loopback；
PREAUTHORIZED 要求 provider_ref 命中 cloud_provider_refs；APPROVAL 远程尚未接入
模型调用审批消费前一律失败关闭。部署层 RING_CLOUD_MODE 与配置取更严者。

Harness 服务名（ring-kernel）不是云身份，须解析为冻结 Profile 的 local/cloud provider。
"""

from __future__ import annotations

from urllib.parse import urlparse

_MODE_RANK = {"DENY": 0, "APPROVAL": 1, "PREAUTHORIZED": 2}
# Runner/Harness 桥身份；不得当作云 provider_ref 落库
HARNESS_SERVICE_PROVIDER_REFS = frozenset({"ring-kernel", "ring-kernel-bridge"})


class CloudModeDenied(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def is_loopback_base(base_url: str) -> bool:
    """仅 127.0.0.1 / localhost / ::1 视为本机；禁止靠变量名推断。"""
    raw = (base_url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def effective_cloud_mode(profile_mode: str, deployment_mode: str) -> str:
    """取更严模式（DENY < APPROVAL < PREAUTHORIZED）。"""
    p = (profile_mode or "DENY").strip().upper()
    d = (deployment_mode or "DENY").strip().upper()
    if p not in _MODE_RANK:
        raise CloudModeDenied(f"非法 cloud_mode={profile_mode!r}")
    if d not in _MODE_RANK:
        raise CloudModeDenied(f"非法部署 RING_CLOUD_MODE={deployment_mode!r}")
    return p if _MODE_RANK[p] <= _MODE_RANK[d] else d


def resolve_frozen_provider_ref(
    requested: str,
    *,
    cloud_mode: str,
    local_provider_ref: str,
    cloud_provider_refs: list[str] | tuple[str, ...] | None,
) -> str:
    """将 Harness 服务名解析为冻结 Profile 的真实 provider_ref。"""
    req = (requested or "").strip()
    local = (local_provider_ref or "").strip()
    refs = [str(x) for x in (cloud_provider_refs or [])]
    mode = (cloud_mode or "DENY").strip().upper()
    if req and req not in HARNESS_SERVICE_PROVIDER_REFS:
        return req
    if mode == "DENY":
        if not local:
            raise CloudModeDenied("DENY 模式缺少 local_provider_ref")
        return local
    if mode == "PREAUTHORIZED":
        if len(refs) == 1:
            return refs[0]
        raise CloudModeDenied(
            "PREAUTHORIZED 且调用方未显式指定云 provider 时，"
            "cloud_provider_refs 必须恰好一项以便解析 Harness 服务名"
        )
    raise CloudModeDenied(
        "cloud_mode=APPROVAL 的模型调用尚未接入审批消费；禁止自动解析 provider"
    )


def assert_provider_model_matches_profile(
    *,
    provider_ref: str,
    model_id: str,
    local_provider_ref: str,
    cloud_provider_refs: list[str] | tuple[str, ...] | None,
    profile_model_id: str,
) -> None:
    """调用方不得改写冻结 Profile 的 provider/model。"""
    allowed = {str(local_provider_ref)} | {str(x) for x in (cloud_provider_refs or [])}
    if provider_ref not in allowed:
        raise CloudModeDenied(
            f"provider_ref={provider_ref!r} 不在冻结 Profile 允许集中"
        )
    if model_id != profile_model_id:
        raise CloudModeDenied(
            f"model_id={model_id!r} 与冻结 Profile.model_id={profile_model_id!r} 不一致"
        )


def assert_chat_endpoint_allowed(
    *,
    base_url: str,
    provider_ref: str,
    cloud_mode: str,
    cloud_provider_refs: list[str] | tuple[str, ...] | None,
    deployment_cloud_mode: str = "DENY",
    approval_id: str | None = None,
) -> None:
    """在进入 DISPATCHED / 真实 HTTP 外呼前调用；失败关闭，不静默降级。"""
    mode = effective_cloud_mode(cloud_mode, deployment_cloud_mode)
    loopback = is_loopback_base(base_url)
    if mode == "DENY":
        if not loopback:
            raise CloudModeDenied(
                "cloud_mode=DENY 禁止非 loopback 模型端点；"
                "开发切云须 ModelProfile.cloud_mode=PREAUTHORIZED 且部署 RING_CLOUD_MODE 允许"
            )
        return
    if loopback:
        return
    refs = list(cloud_provider_refs or [])
    if mode == "PREAUTHORIZED":
        if not provider_ref or provider_ref not in refs:
            raise CloudModeDenied(
                "cloud_mode=PREAUTHORIZED 要求 provider_ref 命中 cloud_provider_refs"
            )
        return
    # APPROVAL 远程：字符串 approval_id 不足；完整消费链未接前失败关闭
    _ = approval_id
    raise CloudModeDenied(
        "cloud_mode=APPROVAL 的远程模型调用尚未接入审批消费；禁止外呼"
    )
