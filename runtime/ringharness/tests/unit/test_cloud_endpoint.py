"""cloud_endpoint 单测：不触网、不读密钥。"""

import pytest
from control_kernel.domain.cloud_endpoint import (
    CloudModeDenied,
    assert_chat_endpoint_allowed,
    effective_cloud_mode,
    is_loopback_base,
    resolve_frozen_provider_ref,
)


def test_loopback_detection():
    assert is_loopback_base("http://127.0.0.1:8001")
    assert is_loopback_base("http://localhost:8001/v1")
    assert is_loopback_base("http://[::1]:8001")
    assert not is_loopback_base("https://api.deepseek.com")
    assert not is_loopback_base("https://api.openai.com/v1")


def test_effective_mode_takes_stricter():
    assert effective_cloud_mode("PREAUTHORIZED", "DENY") == "DENY"
    assert effective_cloud_mode("DENY", "PREAUTHORIZED") == "DENY"
    assert effective_cloud_mode("APPROVAL", "PREAUTHORIZED") == "APPROVAL"
    assert effective_cloud_mode("PREAUTHORIZED", "PREAUTHORIZED") == "PREAUTHORIZED"


def test_deny_allows_loopback():
    assert_chat_endpoint_allowed(
        base_url="http://127.0.0.1:8001",
        provider_ref="pm2:omlx-flashnext",
        cloud_mode="DENY",
        cloud_provider_refs=[],
        deployment_cloud_mode="DENY",
    )


def test_preauthorized_requires_provider_ref_hit():
    with pytest.raises(CloudModeDenied):
        assert_chat_endpoint_allowed(
            base_url="https://api.deepseek.com",
            provider_ref="deepseek:api",
            cloud_mode="PREAUTHORIZED",
            cloud_provider_refs=[],
            deployment_cloud_mode="PREAUTHORIZED",
        )
    assert_chat_endpoint_allowed(
        base_url="https://api.deepseek.com",
        provider_ref="deepseek:api",
        cloud_mode="PREAUTHORIZED",
        cloud_provider_refs=["deepseek:api"],
        deployment_cloud_mode="PREAUTHORIZED",
    )


def test_deployment_deny_overrides_profile_preauthorized():
    with pytest.raises(CloudModeDenied):
        assert_chat_endpoint_allowed(
            base_url="https://api.deepseek.com",
            provider_ref="deepseek:api",
            cloud_mode="PREAUTHORIZED",
            cloud_provider_refs=["deepseek:api"],
            deployment_cloud_mode="DENY",
        )


def test_approval_remote_fail_closed_until_wired():
    with pytest.raises(CloudModeDenied):
        assert_chat_endpoint_allowed(
            base_url="https://api.deepseek.com",
            provider_ref="deepseek:api",
            cloud_mode="APPROVAL",
            cloud_provider_refs=["deepseek:api"],
            deployment_cloud_mode="APPROVAL",
            approval_id="already-approved",
        )


def test_harness_service_name_resolves_to_frozen_provider():
    assert (
        resolve_frozen_provider_ref(
            "ring-kernel",
            cloud_mode="DENY",
            local_provider_ref="pm2:omlx-flashnext",
            cloud_provider_refs=[],
        )
        == "pm2:omlx-flashnext"
    )
    assert (
        resolve_frozen_provider_ref(
            "ring-kernel",
            cloud_mode="PREAUTHORIZED",
            local_provider_ref="pm2:omlx-flashnext",
            cloud_provider_refs=["deepseek:api"],
        )
        == "deepseek:api"
    )
