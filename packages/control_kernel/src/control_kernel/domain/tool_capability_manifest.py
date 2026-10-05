"""M4 ToolCapabilityManifest：工具登记契约与 prepare 门禁（digest C §3–5 / Top 5 #3）。

未登记、schema 漂移、越域 path/URL、超大响应、安全参数被强转、危险工具组合
一律失败关闭；禁止静默 clamp。本模块不做 Goal DONE。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import Field

from ..protocols.effects import EffectScope, ReplayClass
from ..protocols.goals import Digest, PositiveInt, Text
from ..protocols.projects import Contract

EffectClass = Literal["READ", "REVERSIBLE", "IRREVERSIBLE"]
IdempotencyMode = Literal["NONE", "SAME_EFFECT_ID", "NATURAL_KEY"]
ReconcileStrategy = Literal["FORBIDDEN", "REQUIRED_ON_UNKNOWN", "OPTIONAL"]


class ToolCapabilityRejected(Exception):
    """工具能力门禁拒绝；code 供 API/Runner 映射。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class ToolCapabilityManifest(Contract):
    """版本化工具能力声明；Kernel 登记权威，Harness 仅执行已登记结果。"""

    tool_ref: Text = Field(min_length=1, max_length=200)
    schema_digest: Digest
    effect_class: EffectClass
    replay_class: ReplayClass
    scope: EffectScope
    idempotency: IdempotencyMode
    reconcile_strategy: ReconcileStrategy
    timeout_seconds: PositiveInt
    required_param_keys: list[str] = Field(default_factory=list)
    allowed_param_keys: list[str] = Field(default_factory=list)
    path_param_keys: list[str] = Field(default_factory=list)
    url_param_keys: list[str] = Field(default_factory=list)
    # 金额/路径/次数等：禁止自动改写，只能 schema 失败关闭
    no_clamp_param_keys: list[str] = Field(default_factory=list)
    max_response_bytes: PositiveInt
    max_argument_bytes: PositiveInt = 10_000


def compute_schema_digest(schema: Mapping[str, Any]) -> Digest:
    """对参数 schema 规范 JSON 做稳定 digest（与 Content sha256 风格一致）。"""
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


# read_file 冻结 schema：仅 path，禁止额外字段（与 Runner canonicalize 对齐）
READ_FILE_PARAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"path": {"type": "string", "minLength": 1}},
    "required": ["path"],
    "additionalProperties": False,
}

READ_FILE_SCHEMA_DIGEST = compute_schema_digest(READ_FILE_PARAM_SCHEMA)

# write_file：path + content（UTF-8 文本）；内容上限由 max_argument_bytes 约束
WRITE_FILE_PARAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "content": {"type": "string"},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}

WRITE_FILE_SCHEMA_DIGEST = compute_schema_digest(WRITE_FILE_PARAM_SCHEMA)

# run_tests：仅 suite 枚举 → 固定 argv；禁止 command
# public=Executor 可见；auditor=含 tests_hidden（仅验证活动）
RUN_TESTS_PARAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "suite": {"type": "string", "enum": ["public", "auditor"]},
    },
    "required": ["suite"],
    "additionalProperties": False,
}

RUN_TESTS_SCHEMA_DIGEST = compute_schema_digest(RUN_TESTS_PARAM_SCHEMA)

ALLOWED_RUN_TESTS_SUITES: frozenset[str] = frozenset({"public", "auditor"})

# git_diff：无参数；固定 git argv，禁止 command
GIT_DIFF_PARAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

GIT_DIFF_SCHEMA_DIGEST = compute_schema_digest(GIT_DIFF_PARAM_SCHEMA)

# seal_candidate：仅 verification_profile_ids；禁止自填 files/command
SEAL_CANDIDATE_PARAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verification_profile_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        },
    },
    "required": ["verification_profile_ids"],
    "additionalProperties": False,
}

SEAL_CANDIDATE_SCHEMA_DIGEST = compute_schema_digest(SEAL_CANDIDATE_PARAM_SCHEMA)

# 仅用于组合门测/未来登记的危险工具名（当前不在可调度目录）
_DANGEROUS_COMBOS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"read_secret", "http_fetch"}),
        frozenset({"read_file", "http_fetch"}),
        frozenset({"read_secret", "shell_exec"}),
        frozenset({"write_file", "shell_exec"}),
        frozenset({"run_tests", "shell_exec"}),
        frozenset({"git_diff", "shell_exec"}),
        frozenset({"seal_candidate", "shell_exec"}),
    }
)


def builtin_manifests() -> dict[str, ToolCapabilityManifest]:
    """内建受信工具目录；未列入即未登记。"""
    read_file = ToolCapabilityManifest(
        tool_ref="read_file",
        schema_digest=READ_FILE_SCHEMA_DIGEST,
        effect_class="READ",
        replay_class="READ_ONLY",
        scope="ENGINEERING",
        idempotency="SAME_EFFECT_ID",
        reconcile_strategy="REQUIRED_ON_UNKNOWN",
        timeout_seconds=30,
        required_param_keys=["path"],
        allowed_param_keys=["path"],
        path_param_keys=["path"],
        url_param_keys=[],
        no_clamp_param_keys=["path"],
        max_response_bytes=8 * 1024 * 1024,
        max_argument_bytes=10_000,
    )
    write_file = ToolCapabilityManifest(
        tool_ref="write_file",
        schema_digest=WRITE_FILE_SCHEMA_DIGEST,
        effect_class="REVERSIBLE",
        replay_class="IDEMPOTENT",
        scope="ENGINEERING",
        idempotency="SAME_EFFECT_ID",
        reconcile_strategy="REQUIRED_ON_UNKNOWN",
        timeout_seconds=60,
        required_param_keys=["path", "content"],
        allowed_param_keys=["path", "content"],
        path_param_keys=["path"],
        url_param_keys=[],
        no_clamp_param_keys=["path", "content"],
        max_response_bytes=2 * 1024 * 1024,
        max_argument_bytes=512_000,
    )
    run_tests = ToolCapabilityManifest(
        tool_ref="run_tests",
        schema_digest=RUN_TESTS_SCHEMA_DIGEST,
        effect_class="READ",
        replay_class="READ_ONLY",
        scope="ENGINEERING",
        idempotency="SAME_EFFECT_ID",
        reconcile_strategy="REQUIRED_ON_UNKNOWN",
        timeout_seconds=120,
        required_param_keys=["suite"],
        allowed_param_keys=["suite"],
        path_param_keys=[],
        url_param_keys=[],
        no_clamp_param_keys=["suite"],
        max_response_bytes=1 * 1024 * 1024,
        max_argument_bytes=2_000,
    )
    git_diff = ToolCapabilityManifest(
        tool_ref="git_diff",
        schema_digest=GIT_DIFF_SCHEMA_DIGEST,
        effect_class="READ",
        replay_class="READ_ONLY",
        scope="ENGINEERING",
        idempotency="SAME_EFFECT_ID",
        reconcile_strategy="REQUIRED_ON_UNKNOWN",
        timeout_seconds=30,
        required_param_keys=[],
        allowed_param_keys=[],
        path_param_keys=[],
        url_param_keys=[],
        no_clamp_param_keys=[],
        max_response_bytes=1 * 1024 * 1024,
        max_argument_bytes=2_000,
    )
    seal_candidate = ToolCapabilityManifest(
        tool_ref="seal_candidate",
        schema_digest=SEAL_CANDIDATE_SCHEMA_DIGEST,
        effect_class="IRREVERSIBLE",
        replay_class="IDEMPOTENT",
        scope="ENGINEERING",
        idempotency="SAME_EFFECT_ID",
        reconcile_strategy="REQUIRED_ON_UNKNOWN",
        timeout_seconds=120,
        required_param_keys=["verification_profile_ids"],
        allowed_param_keys=["verification_profile_ids"],
        path_param_keys=[],
        url_param_keys=[],
        no_clamp_param_keys=["verification_profile_ids"],
        max_response_bytes=1 * 1024 * 1024,
        max_argument_bytes=4_000,
    )
    return {
        read_file.tool_ref: read_file,
        write_file.tool_ref: write_file,
        run_tests.tool_ref: run_tests,
        git_diff.tool_ref: git_diff,
        seal_candidate.tool_ref: seal_candidate,
    }


TOOL_CAPABILITY_MANIFESTS: dict[str, ToolCapabilityManifest] = builtin_manifests()

# 兼容旧 TOOL_REGISTRY 形状
TOOL_REGISTRY: dict[str, dict[str, str]] = {
    ref: {"replay_class": m.replay_class, "scope": m.scope}
    for ref, m in TOOL_CAPABILITY_MANIFESTS.items()
}


def require_manifest(tool_ref: str) -> ToolCapabilityManifest:
    manifest = TOOL_CAPABILITY_MANIFESTS.get(tool_ref)
    if manifest is None:
        raise ToolCapabilityRejected("TOOL_NOT_REGISTERED", f"工具未在受信目录登记：{tool_ref}")
    return manifest


def assert_schema_digest(manifest: ToolCapabilityManifest, claimed: Digest | None) -> None:
    """漂移 schema digest 失败关闭；None 表示调用方未声明（由 Kernel 绑定现行 digest）。"""
    if claimed is None:
        return
    if claimed != manifest.schema_digest:
        raise ToolCapabilityRejected(
            "TOOL_SCHEMA_DRIFT",
            f"tool_schema_digest 与登记 manifest 不一致（tool={manifest.tool_ref}）",
        )


def assert_parameters_against_manifest(
    manifest: ToolCapabilityManifest,
    parameters: Mapping[str, Any],
    *,
    raw_parameters: Mapping[str, Any] | None = None,
) -> None:
    """参数键集合与类型门禁；禁止对 no_clamp 键做静默改写。"""
    keys = set(parameters.keys())
    allowed = set(manifest.allowed_param_keys)
    required = set(manifest.required_param_keys)
    missing = required - keys
    if missing:
        raise ToolCapabilityRejected(
            "TOOL_PARAM_INVALID",
            f"缺少必填参数：{', '.join(sorted(missing))}",
        )
    unknown = keys - allowed
    if unknown:
        raise ToolCapabilityRejected(
            "TOOL_PARAM_INVALID",
            f"未知参数（禁止自动剥离）：{', '.join(sorted(unknown))}",
        )

    raw = raw_parameters if raw_parameters is not None else parameters
    for key in manifest.no_clamp_param_keys:
        if key in raw and key in parameters and raw[key] != parameters[key]:
            raise ToolCapabilityRejected(
                "TOOL_PARAM_CLAMP_FORBIDDEN",
                f"禁止强转安全敏感参数 {key}；须 schema 失败关闭",
            )

    for key in manifest.path_param_keys:
        value = parameters.get(key)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ToolCapabilityRejected(
                "TOOL_PARAM_INVALID",
                f"路径参数 {key} 必须为非空且无首尾空白的字符串",
            )

    for key in manifest.url_param_keys:
        value = parameters.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ToolCapabilityRejected(
                "TOOL_PARAM_INVALID",
                f"URL 参数 {key} 必须为非空字符串",
            )


def assert_run_tests_suite(parameters: Mapping[str, Any]) -> None:
    """run_tests.suite 必须落在批准枚举；拒绝自由 command 旁路。"""
    suite = parameters.get("suite")
    if not isinstance(suite, str) or suite not in ALLOWED_RUN_TESTS_SUITES:
        raise ToolCapabilityRejected(
            "TOOL_PARAM_INVALID",
            f"suite 必须为 {sorted(ALLOWED_RUN_TESTS_SUITES)} 之一",
        )


VERIFY_ACTIVITY_KINDS: frozenset[str] = frozenset(
    {"AUDIT", "FINALIZE", "VALIDATE_SKILL"}
)


def assert_run_tests_suite_for_activity(
    activity_kind: str,
    parameters: Mapping[str, Any],
) -> None:
    """
    三权 suite 门：EXECUTE 仅 public；验证活动仅 auditor（含 tests_hidden）。
    """
    assert_run_tests_suite(parameters)
    suite = parameters["suite"]
    if activity_kind == "EXECUTE" and suite == "auditor":
        raise ToolCapabilityRejected(
            "TOOL_PARAM_INVALID",
            "EXECUTE 禁止 suite=auditor（tests_hidden 仅验证活动）",
        )
    if activity_kind in VERIFY_ACTIVITY_KINDS and suite != "auditor":
        raise ToolCapabilityRejected(
            "TOOL_PARAM_INVALID",
            f"{activity_kind} 须 suite=auditor（禁止仅用 public 冒充独立验收）",
        )


_PATH_ESCAPE = re.compile(r"(^|/)\.\.(/|$)")


def assert_path_within_policy(
    relative_path: str,
    *,
    allowed_paths: Sequence[str],
    protected_paths: Sequence[str] = (),
) -> None:
    """Kernel prepare 侧路径门禁（与 Broker 沙箱同语义，不信任请求自报）。"""
    if not relative_path or relative_path.strip() != relative_path:
        raise ToolCapabilityRejected("TOOL_PATH_DENIED", "路径为空或含首尾空白")
    if relative_path.startswith(("/", "~")):
        raise ToolCapabilityRejected("TOOL_PATH_DENIED", "禁止绝对路径")
    if "\\" in relative_path:
        raise ToolCapabilityRejected("TOOL_PATH_DENIED", "禁止反斜杠路径")
    if _PATH_ESCAPE.search(relative_path) or ".." in relative_path.split("/"):
        raise ToolCapabilityRejected("TOOL_PATH_DENIED", "禁止父目录逃逸")

    norm = relative_path.replace("\\", "/")
    for pattern in protected_paths:
        if _glob_match(norm, pattern):
            raise ToolCapabilityRejected(
                "TOOL_PATH_DENIED",
                f"路径落在 protected_paths：{pattern}",
            )
    if not allowed_paths:
        raise ToolCapabilityRejected("TOOL_PATH_DENIED", "策略未配置 allowed_paths")
    if not any(_glob_match(norm, p) for p in allowed_paths):
        raise ToolCapabilityRejected(
            "TOOL_PATH_DENIED",
            "路径不在策略 allowed_paths 内",
        )


def assert_url_within_network_allowlist(
    url: str,
    *,
    network_allowlist: Sequence[str],
) -> None:
    """出站 URL 必须命中 network_allowlist；空名单 = 拒绝全部出站。"""
    if not network_allowlist:
        raise ToolCapabilityRejected(
            "TOOL_NETWORK_DENIED",
            "策略 network_allowlist 为空，拒绝出站",
        )
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise ToolCapabilityRejected("TOOL_NETWORK_DENIED", "URL 无法解析") from exc
    if parsed.scheme not in ("http", "https"):
        raise ToolCapabilityRejected("TOOL_NETWORK_DENIED", "仅允许 http/https")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ToolCapabilityRejected("TOOL_NETWORK_DENIED", "URL 缺少主机名")
    for entry in network_allowlist:
        rule = entry.strip().lower()
        if not rule:
            continue
        if rule.startswith("*."):
            suffix = rule[1:]  # .example.com
            if host.endswith(suffix) or host == rule[2:]:
                return
        if host == rule or url.lower().startswith(rule):
            return
    raise ToolCapabilityRejected(
        "TOOL_NETWORK_DENIED",
        f"主机 {host} 不在 network_allowlist",
    )


def assert_response_within_ceiling(size_bytes: int, manifest: ToolCapabilityManifest) -> None:
    if size_bytes < 0:
        raise ToolCapabilityRejected("TOOL_RESPONSE_TOO_LARGE", "响应大小非法")
    if size_bytes > manifest.max_response_bytes:
        raise ToolCapabilityRejected(
            "TOOL_RESPONSE_TOO_LARGE",
            f"响应 {size_bytes} 字节超过 manifest 上限 {manifest.max_response_bytes}",
        )


def assert_argument_bytes_within_ceiling(
    raw_bytes: int, manifest: ToolCapabilityManifest
) -> None:
    if raw_bytes > manifest.max_argument_bytes:
        raise ToolCapabilityRejected(
            "TOOL_ARGUMENTS_TOO_LARGE",
            f"参数 {raw_bytes} 字节超过上限 {manifest.max_argument_bytes}",
        )


def assert_tool_combination_allowed(tool_refs: Sequence[str]) -> None:
    """危险组合门：同时具备敏感读 + 出站/执行等能力时拒绝。"""
    selected = set(tool_refs)
    for combo in _DANGEROUS_COMBOS:
        if combo <= selected:
            raise ToolCapabilityRejected(
                "TOOL_COMBO_FORBIDDEN",
                f"禁止的工具组合：{', '.join(sorted(combo))}",
            )


def validate_skill_tool_selection_fixture(
    *,
    required_tools: Sequence[str],
    must_allow: Sequence[str],
    must_deny: Sequence[str],
) -> None:
    """VALIDATE_SKILL 用：代表性请求应选中允许工具、拒绝近似危险工具。"""
    have = set(required_tools)
    for name in must_allow:
        if name not in have:
            raise ToolCapabilityRejected(
                "TOOL_FIXTURE_MISS",
                f"Skill 工具 fixture 未包含应允许工具：{name}",
            )
    for name in must_deny:
        if name in have:
            raise ToolCapabilityRejected(
                "TOOL_FIXTURE_DANGER",
                f"Skill 工具 fixture 含应拒绝的危险工具：{name}",
            )
    assert_tool_combination_allowed(list(have))


# 已知危险/未登记工具名：出现在 Skill.required_tools 即 fixture 失败
_SKILL_FIXTURE_DENY_TOOLS: frozenset[str] = frozenset(
    {"http_fetch", "read_secret", "shell_exec", "outbound_http"}
)


def assert_skill_required_tools_admissible(
    required_tools: Sequence[str],
    *,
    policy_allowed_tools: Sequence[str] | None = None,
) -> None:
    """Skill 创建 / VALIDATE_SKILL：required_tools 须已登记、无危险组合、无 fixture 禁工。

    空集合允许（无工具 Skill）；非空则每项必须在 ToolCapabilityManifest 目录中。
    若提供 policy_allowed_tools，required 必须是其子集。
    """
    tools = list(required_tools)
    if not tools:
        return

    validate_skill_tool_selection_fixture(
        required_tools=tools,
        must_allow=[],
        must_deny=[t for t in tools if t in _SKILL_FIXTURE_DENY_TOOLS],
    )
    for name in tools:
        require_manifest(name)

    if policy_allowed_tools is not None:
        allowed = set(policy_allowed_tools)
        for name in tools:
            if name not in allowed:
                raise ToolCapabilityRejected(
                    "TOOL_POLICY_DENIED",
                    f"Skill required_tools 含策略未允许工具：{name}",
                )


def parse_tool_input_artifact(raw: bytes) -> tuple[str | None, Digest | None, dict[str, Any]]:
    """解析输入工件：ToolPayload 或遗留 {\"path\":...} 参数对象。"""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolCapabilityRejected(
            "TOOL_PARAM_INVALID",
            "输入工件不是合法 JSON",
        ) from exc
    if not isinstance(parsed, dict) or isinstance(parsed, list):
        raise ToolCapabilityRejected("TOOL_PARAM_INVALID", "输入工件必须为 JSON 对象")

    if "parameters" in parsed or "tool_schema_digest" in parsed or "tool_ref" in parsed:
        tool_ref = parsed.get("tool_ref")
        digest = parsed.get("tool_schema_digest")
        params = parsed.get("parameters")
        if not isinstance(tool_ref, str):
            raise ToolCapabilityRejected("TOOL_PARAM_INVALID", "ToolPayload 缺少 tool_ref")
        if not isinstance(digest, str):
            raise ToolCapabilityRejected(
                "TOOL_PARAM_INVALID",
                "ToolPayload 缺少 tool_schema_digest",
            )
        if not isinstance(params, dict):
            raise ToolCapabilityRejected("TOOL_PARAM_INVALID", "ToolPayload.parameters 须为对象")
        return tool_ref, digest, params

    return None, None, parsed


def admit_tool_prepare(
    *,
    tool_ref: str,
    input_artifact_bytes: bytes | None,
    policy_allowed_tools: Sequence[str],
    policy_allowed_paths: Sequence[str],
    policy_protected_paths: Sequence[str] = (),
    policy_network_allowlist: Sequence[str] = (),
    sibling_tool_refs: Sequence[str] = (),
) -> ToolCapabilityManifest:
    """prepare_effect 聚合门禁：登记/策略/schema/path/URL/组合。"""
    if tool_ref not in set(policy_allowed_tools):
        raise ToolCapabilityRejected("TOOL_POLICY_DENIED", "策略不允许该工具")

    manifest = require_manifest(tool_ref)
    assert_tool_combination_allowed([*sibling_tool_refs, tool_ref])

    if input_artifact_bytes is None:
        # 无对象仓时仍拒绝未登记；参数级校验留给有字节的路径（失败关闭不静默放行参数）
        return manifest

    assert_argument_bytes_within_ceiling(len(input_artifact_bytes), manifest)
    payload_tool, claimed_digest, parameters = parse_tool_input_artifact(input_artifact_bytes)
    if payload_tool is not None and payload_tool != tool_ref:
        raise ToolCapabilityRejected(
            "TOOL_PARAM_INVALID",
            f"工件 tool_ref={payload_tool} 与请求 {tool_ref} 不一致",
        )
    assert_schema_digest(manifest, claimed_digest)
    assert_parameters_against_manifest(manifest, parameters)
    if tool_ref == "run_tests":
        assert_run_tests_suite(parameters)

    for key in manifest.path_param_keys:
        assert_path_within_policy(
            str(parameters[key]),
            allowed_paths=policy_allowed_paths,
            protected_paths=policy_protected_paths,
        )
    for key in manifest.url_param_keys:
        assert_url_within_network_allowlist(
            str(parameters[key]),
            network_allowlist=policy_network_allowlist,
        )
    return manifest


def _glob_match(path: str, pattern: str) -> bool:
    import fnmatch

    pat = pattern.replace("\\", "/")
    if fnmatch.fnmatch(path, pat):
        return True
    return bool(pat.endswith("/**") and (path == pat[:-3] or path.startswith(pat[:-2])))


def registry_meta(tool_ref: str) -> dict[str, str]:
    """供 effect_intents 写入 replay_class/scope。"""
    m = require_manifest(tool_ref)
    return {"replay_class": m.replay_class, "scope": m.scope}
