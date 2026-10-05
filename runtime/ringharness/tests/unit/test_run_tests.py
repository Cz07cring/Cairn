"""run_tests 无法由候选输入绕过的安全边界。"""

from __future__ import annotations

from pathlib import Path

import pytest
from execution_broker.run_tests import (
    parse_run_tests_input,
    resolve_suite_argv,
)


def test_auditor_suite_requires_tests_hidden(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    with pytest.raises(ValueError, match="tests_hidden"):
        resolve_suite_argv("auditor", workspace_root=tmp_path)
    (tmp_path / "tests_hidden").mkdir()
    argv = resolve_suite_argv("auditor", workspace_root=tmp_path)
    assert argv[-2:] == ("tests/", "tests_hidden/")


def test_parse_rejects_freeform_command() -> None:
    with pytest.raises(ValueError, match="command|suite"):
        parse_run_tests_input(b'{"command":"rm -rf /"}')
    with pytest.raises(ValueError, match="未知 suite"):
        parse_run_tests_input(b'{"suite":"shell_me"}')
