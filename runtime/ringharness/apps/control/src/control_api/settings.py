"""Missing identity configuration fails closed; generation mode never needs credentials."""

from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RING_", extra="ignore")
    database_url: SecretStr | None = None
    jwt_public_key: str | None = None
    jwt_issuer: str = "ringharness"
    jwt_audience: str = "ring-api"
    cursor_secret: SecretStr | None = None
    repository_refs: list[str] = []
    # 别名 → 本机 git 仓库绝对路径；用于 attempt worktree 检出 base_commit。
    repository_paths: dict[str, str] = Field(default_factory=dict)
    s3_endpoint: str | None = None
    s3_bucket: str | None = None
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_region: str = "us-east-1"
    artifact_max_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=64 * 1024 * 1024)
    # doc/08：RING_LEASE_TTL_SECONDS / RING_HEARTBEAT_SECONDS；TTL≥3×心跳
    lease_ttl_seconds: int = Field(default=90, ge=30, le=600)
    heartbeat_seconds: int = Field(default=15, ge=5, le=120)
    local_qwen_base: str = "http://127.0.0.1:8001"
    local_qwen_api_key: SecretStr | None = None
    # live dispatch 补全上限的**下限**。推理模型（deepseek-flash / deepseek-v4-pro 同属此列）
    # 先消耗 reasoning token；上限过小时推理吃满、`content` 为空且 `finish_reason=length`，
    # 外部只看到「模型调通了却解析不出正文」。实测同一 PLAN 提示词：
    # 512 上限 → 3/3 空；1024 → 1/3 空；2048 → 0/3 空。故取 2048 为下限。
    model_min_output_tokens: int = Field(default=2048, ge=256, le=32768)
    # 部署层云闸门；与 ModelProfile.cloud_mode 取更严者。默认 DENY。
    cloud_mode: str = "DENY"
    # 浏览器 session / OIDC；缺任一项则 login/callback 失败关闭。
    session_secret: SecretStr | None = None
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_redirect_uri: str | None = None
    auth_return_to_origins: list[str] = Field(default_factory=list)
    cookie_secure: bool = False

    @model_validator(mode="after")
    def _lease_ttl_covers_three_heartbeats(self) -> Self:
        """doc/08：TTL≥3×心跳；防止可配旋钮被设成必失租组合。"""
        min_ttl = 3 * int(self.heartbeat_seconds)
        if int(self.lease_ttl_seconds) < min_ttl:
            raise ValueError(
                f"RING_LEASE_TTL_SECONDS={self.lease_ttl_seconds} "
                f"须 ≥ 3×RING_HEARTBEAT_SECONDS（{min_ttl}）"
            )
        return self
