"""ExecutionBroker：沙箱工具执行（不直连业务库写权限）。

长驻入口见 ``python -m execution_broker`` / ``python -m broker_app``；
完成判定与 Goal/Task 调度权威仍在 ControlKernel。
"""

from .git_diff import GitDiffResult, execute_git_diff, parse_git_diff_input
from .host import (
    run_git_diff_effect,
    run_read_file_effect,
    run_run_tests_effect,
    run_seal_candidate_effect,
    run_write_file_effect,
)
from .materialize_candidate import (
    MaterializeRejected,
    attach_holdout_tests,
    make_tree_read_only,
    materialize_candidate_worktree,
)
from .paths import PathRejected, resolve_workspace_path
from .read_file import ReadFileResult, execute_read_file, parse_read_file_input
from .run_tests import (
    APPROVED_SUITES,
    RunTestsResult,
    execute_run_tests,
    parse_run_tests_input,
    resolve_suite_argv,
)
from .seal_candidate import (
    SealCandidateResult,
    build_workspace_snapshot,
    execute_seal_candidate_prepare,
    parse_seal_candidate_input,
)
from .service import (
    BrokerSettings,
    health,
    load_settings,
    poll_dispatchable,
    process_dispatchable_once,
    refuse_mark_goal_done,
    run_loop,
)
from .worktree import (
    WorkspaceUnavailable,
    attempt_workspace_path,
    ensure_attempt_workspace,
    seed_workspace_at_commit,
    workspace_base,
)
from .write_file import WriteFileResult, execute_write_file, parse_write_file_input

__all__ = [
    "APPROVED_SUITES",
    "BrokerSettings",
    "GitDiffResult",
    "MaterializeRejected",
    "PathRejected",
    "ReadFileResult",
    "RunTestsResult",
    "SealCandidateResult",
    "WorkspaceUnavailable",
    "WriteFileResult",
    "attach_holdout_tests",
    "attempt_workspace_path",
    "build_workspace_snapshot",
    "ensure_attempt_workspace",
    "execute_git_diff",
    "execute_read_file",
    "execute_run_tests",
    "execute_seal_candidate_prepare",
    "execute_write_file",
    "health",
    "load_settings",
    "make_tree_read_only",
    "materialize_candidate_worktree",
    "parse_git_diff_input",
    "parse_read_file_input",
    "parse_run_tests_input",
    "parse_seal_candidate_input",
    "parse_write_file_input",
    "poll_dispatchable",
    "process_dispatchable_once",
    "refuse_mark_goal_done",
    "resolve_suite_argv",
    "resolve_workspace_path",
    "run_git_diff_effect",
    "run_loop",
    "run_read_file_effect",
    "run_run_tests_effect",
    "run_seal_candidate_effect",
    "run_write_file_effect",
    "seed_workspace_at_commit",
    "workspace_base",
]
