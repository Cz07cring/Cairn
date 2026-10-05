"""ToolCapabilityManifest 门禁：未登记/漂移/越域/超大/强转/危险组合。"""

from __future__ import annotations

import json

import pytest
from control_kernel.domain import tool_capability_manifest as tcm
from control_kernel.domain.tool_capability_manifest import (
    READ_FILE_SCHEMA_DIGEST,
    ToolCapabilityRejected,
    admit_tool_prepare,
    assert_argument_bytes_within_ceiling,
    assert_parameters_against_manifest,
    assert_path_within_policy,
    assert_response_within_ceiling,
    assert_tool_combination_allowed,
    assert_url_within_network_allowlist,
    parse_tool_input_artifact,
    require_manifest,
    validate_skill_tool_selection_fixture,
)


def test_write_file_manifest_registered() -> None:
    m = require_manifest("write_file")
    assert m.schema_digest == tcm.WRITE_FILE_SCHEMA_DIGEST
    assert m.effect_class == "REVERSIBLE"
    assert m.replay_class == "IDEMPOTENT"
    assert m.idempotency == "SAME_EFFECT_ID"
    assert set(m.required_param_keys) == {"path", "content"}
    assert_parameters_against_manifest(
        m, {"path": "order_service/store.py", "content": "fixed\n"}
    )
    with pytest.raises(ToolCapabilityRejected) as ei:
        assert_parameters_against_manifest(m, {"path": "a.py"})
    assert ei.value.code == "TOOL_PARAM_INVALID"


def test_run_tests_manifest_registered() -> None:
    m = require_manifest("run_tests")
    assert m.schema_digest == tcm.RUN_TESTS_SCHEMA_DIGEST
    assert m.effect_class == "READ"
    assert m.replay_class == "READ_ONLY"
    assert_parameters_against_manifest(m, {"suite": "public"})
    assert_parameters_against_manifest(m, {"suite": "auditor"})
    tcm.assert_run_tests_suite({"suite": "public"})
    tcm.assert_run_tests_suite({"suite": "auditor"})
    with pytest.raises(ToolCapabilityRejected) as ei:
        tcm.assert_run_tests_suite({"suite": "secret"})
    assert ei.value.code == "TOOL_PARAM_INVALID"
    with pytest.raises(ToolCapabilityRejected):
        assert_parameters_against_manifest(m, {"suite": "public", "command": "x"})
    with pytest.raises(ToolCapabilityRejected):
        assert_parameters_against_manifest(m, {"command": "rm -rf /"})


def test_git_diff_manifest_registered() -> None:
    m = require_manifest("git_diff")
    assert m.schema_digest == tcm.GIT_DIFF_SCHEMA_DIGEST
    assert m.effect_class == "READ"
    assert m.replay_class == "READ_ONLY"
    assert_parameters_against_manifest(m, {})
    with pytest.raises(ToolCapabilityRejected):
        assert_parameters_against_manifest(m, {"command": "git push"})
    with pytest.raises(ToolCapabilityRejected):
        assert_parameters_against_manifest(m, {"path": "."})


def test_seal_candidate_manifest_registered() -> None:
    m = require_manifest("seal_candidate")
    assert m.schema_digest == tcm.SEAL_CANDIDATE_SCHEMA_DIGEST
    assert m.effect_class == "IRREVERSIBLE"
    assert m.replay_class == "IDEMPOTENT"
    pid = "00000000-0000-4000-8000-000000000001"
    assert_parameters_against_manifest(m, {"verification_profile_ids": [pid]})
    with pytest.raises(ToolCapabilityRejected):
        assert_parameters_against_manifest(m, {"files": []})
    with pytest.raises(ToolCapabilityRejected):
        assert_parameters_against_manifest(m, {})


def test_params_unknown_and_missing() -> None:
    m = require_manifest("read_file")
    with pytest.raises(ToolCapabilityRejected) as ei:
        assert_parameters_against_manifest(m, {})
    assert ei.value.code == "TOOL_PARAM_INVALID"
    with pytest.raises(ToolCapabilityRejected) as ei:
        assert_parameters_against_manifest(m, {"path": "a.py", "extra": 1})
    assert "未知参数" in ei.value.message


def test_param_clamp_forbidden() -> None:
    m = require_manifest("read_file")
    with pytest.raises(ToolCapabilityRejected) as ei:
        assert_parameters_against_manifest(
            m,
            {"path": "src/safe.py"},
            raw_parameters={"path": "../etc/passwd"},
        )
    assert ei.value.code == "TOOL_PARAM_CLAMP_FORBIDDEN"


def test_path_policy_denies_escape_and_out_of_allowlist() -> None:
    with pytest.raises(ToolCapabilityRejected) as ei:
        assert_path_within_policy("../x", allowed_paths=["src/**"])
    assert ei.value.code == "TOOL_PATH_DENIED"
    with pytest.raises(ToolCapabilityRejected):
        assert_path_within_policy("secret.txt", allowed_paths=["src/**"])
    assert_path_within_policy("src/main.py", allowed_paths=["src/**"])
    with pytest.raises(ToolCapabilityRejected):
        assert_path_within_policy(
            "src/main.py",
            allowed_paths=["src/**"],
            protected_paths=["src/main.py"],
        )


def test_network_allowlist() -> None:
    with pytest.raises(ToolCapabilityRejected) as ei:
        assert_url_within_network_allowlist(
            "https://evil.example/x", network_allowlist=[]
        )
    assert ei.value.code == "TOOL_NETWORK_DENIED"
    assert_url_within_network_allowlist(
        "https://api.example.com/v1",
        network_allowlist=["api.example.com"],
    )
    with pytest.raises(ToolCapabilityRejected):
        assert_url_within_network_allowlist(
            "https://evil.example/x",
            network_allowlist=["api.example.com"],
        )


def test_response_and_argument_ceilings() -> None:
    m = require_manifest("read_file")
    assert_response_within_ceiling(100, m)
    with pytest.raises(ToolCapabilityRejected) as ei:
        assert_response_within_ceiling(m.max_response_bytes + 1, m)
    assert ei.value.code == "TOOL_RESPONSE_TOO_LARGE"
    with pytest.raises(ToolCapabilityRejected) as ei:
        assert_argument_bytes_within_ceiling(m.max_argument_bytes + 1, m)
    assert ei.value.code == "TOOL_ARGUMENTS_TOO_LARGE"


def test_dangerous_combo() -> None:
    with pytest.raises(ToolCapabilityRejected) as ei:
        assert_tool_combination_allowed(["read_file", "http_fetch"])
    assert ei.value.code == "TOOL_COMBO_FORBIDDEN"
    assert_tool_combination_allowed(["read_file"])


def test_skill_tool_fixture() -> None:
    validate_skill_tool_selection_fixture(
        required_tools=["read_file"],
        must_allow=["read_file"],
        must_deny=["http_fetch"],
    )
    with pytest.raises(ToolCapabilityRejected) as ei:
        validate_skill_tool_selection_fixture(
            required_tools=["read_file", "http_fetch"],
            must_allow=["read_file"],
            must_deny=["http_fetch"],
        )
    assert ei.value.code in ("TOOL_FIXTURE_DANGER", "TOOL_COMBO_FORBIDDEN")


def test_parse_legacy_and_tool_payload() -> None:
    raw_tool, dig, params = parse_tool_input_artifact(b'{"path":"src/a.py"}')
    assert raw_tool is None and dig is None and params == {"path": "src/a.py"}
    blob = json.dumps(
        {
            "tool_ref": "read_file",
            "tool_schema_digest": READ_FILE_SCHEMA_DIGEST,
            "parameters": {"path": "src/a.py"},
        }
    ).encode()
    tool, claimed, params = parse_tool_input_artifact(blob)
    assert tool == "read_file" and claimed == READ_FILE_SCHEMA_DIGEST
    assert params == {"path": "src/a.py"}


def test_admit_run_tests_suite_enum() -> None:
    public_blob = json.dumps(
        {
            "tool_ref": "run_tests",
            "tool_schema_digest": tcm.RUN_TESTS_SCHEMA_DIGEST,
            "parameters": {"suite": "public"},
        },
        separators=(",", ":"),
    ).encode()
    auditor_blob = json.dumps(
        {
            "tool_ref": "run_tests",
            "tool_schema_digest": tcm.RUN_TESTS_SCHEMA_DIGEST,
            "parameters": {"suite": "auditor"},
        },
        separators=(",", ":"),
    ).encode()
    bad_blob = json.dumps(
        {
            "tool_ref": "run_tests",
            "tool_schema_digest": tcm.RUN_TESTS_SCHEMA_DIGEST,
            "parameters": {"suite": "hidden"},
        },
        separators=(",", ":"),
    ).encode()
    assert (
        admit_tool_prepare(
            tool_ref="run_tests",
            input_artifact_bytes=public_blob,
            policy_allowed_tools=["run_tests"],
            policy_allowed_paths=["**"],
        ).tool_ref
        == "run_tests"
    )
    assert (
        admit_tool_prepare(
            tool_ref="run_tests",
            input_artifact_bytes=auditor_blob,
            policy_allowed_tools=["run_tests"],
            policy_allowed_paths=["**"],
        ).tool_ref
        == "run_tests"
    )
    with pytest.raises(ToolCapabilityRejected) as ei:
        admit_tool_prepare(
            tool_ref="run_tests",
            input_artifact_bytes=bad_blob,
            policy_allowed_tools=["run_tests"],
            policy_allowed_paths=["**"],
        )
    assert ei.value.code == "TOOL_PARAM_INVALID"


def test_run_tests_suite_for_activity_three_way() -> None:
    tcm.assert_run_tests_suite_for_activity("EXECUTE", {"suite": "public"})
    tcm.assert_run_tests_suite_for_activity("AUDIT", {"suite": "auditor"})
    tcm.assert_run_tests_suite_for_activity("FINALIZE", {"suite": "auditor"})
    with pytest.raises(ToolCapabilityRejected) as ei:
        tcm.assert_run_tests_suite_for_activity("EXECUTE", {"suite": "auditor"})
    assert "auditor" in ei.value.message
    with pytest.raises(ToolCapabilityRejected) as ei2:
        tcm.assert_run_tests_suite_for_activity("AUDIT", {"suite": "public"})
    assert "auditor" in ei2.value.message
    with pytest.raises(ToolCapabilityRejected):
        tcm.assert_run_tests_suite_for_activity("VALIDATE_SKILL", {"suite": "public"})


def test_admit_prepare_happy_and_path_denied() -> None:
    m = admit_tool_prepare(
        tool_ref="read_file",
        input_artifact_bytes=b'{"path":"src/main.py"}',
        policy_allowed_tools=["read_file"],
        policy_allowed_paths=["src/**"],
    )
    assert m.tool_ref == "read_file"
    with pytest.raises(ToolCapabilityRejected) as ei:
        admit_tool_prepare(
            tool_ref="read_file",
            input_artifact_bytes=b'{"path":"etc/passwd"}',
            policy_allowed_tools=["read_file"],
            policy_allowed_paths=["src/**"],
        )
    assert ei.value.code == "TOOL_PATH_DENIED"


def test_admit_schema_drift_via_payload() -> None:
    blob = json.dumps(
        {
            "tool_ref": "read_file",
            "tool_schema_digest": "sha256:" + ("0" * 64),
            "parameters": {"path": "src/a.py"},
        }
    ).encode()
    with pytest.raises(ToolCapabilityRejected) as ei:
        admit_tool_prepare(
            tool_ref="read_file",
            input_artifact_bytes=blob,
            policy_allowed_tools=["read_file"],
            policy_allowed_paths=["src/**"],
        )
    assert ei.value.code == "TOOL_SCHEMA_DRIFT"


def test_assert_skill_required_tools_admissible() -> None:
    from control_kernel.domain.tool_capability_manifest import (
        assert_skill_required_tools_admissible,
    )

    assert_skill_required_tools_admissible([])
    assert_skill_required_tools_admissible(["read_file"])
    assert_skill_required_tools_admissible(
        ["read_file"], policy_allowed_tools=["read_file"]
    )
    with pytest.raises(ToolCapabilityRejected) as ei:
        assert_skill_required_tools_admissible(["http_fetch"])
    assert ei.value.code in ("TOOL_FIXTURE_DANGER", "TOOL_NOT_REGISTERED", "TOOL_COMBO_FORBIDDEN")
    with pytest.raises(ToolCapabilityRejected) as ei:
        assert_skill_required_tools_admissible(["no_such_tool"])
    assert ei.value.code == "TOOL_NOT_REGISTERED"
    with pytest.raises(ToolCapabilityRejected) as ei:
        assert_skill_required_tools_admissible(
            ["read_file"], policy_allowed_tools=["other"]
        )
    assert ei.value.code == "TOOL_POLICY_DENIED"
