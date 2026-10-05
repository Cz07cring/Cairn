"""write_file 路径与输入协议的失败关闭边界。"""

from __future__ import annotations

from pathlib import Path

import pytest
from execution_broker import PathRejected, resolve_workspace_path
from execution_broker.write_file import parse_write_file_input


def test_write_resolve_rejects_escape(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    with pytest.raises(PathRejected):
        resolve_workspace_path(
            tmp_path,
            "../etc/passwd",
            allowed_paths=["src/**"],
            protected_paths=[],
        )
    with pytest.raises(PathRejected):
        resolve_workspace_path(
            tmp_path,
            "src/secret.py",
            allowed_paths=["src/**"],
            protected_paths=["src/secret.py"],
        )


def test_parse_write_file_input_contract() -> None:
    with pytest.raises(ValueError, match="content"):
        parse_write_file_input(b'{"path":"a.py"}')
    with pytest.raises(ValueError, match="path"):
        parse_write_file_input(b'{"content":"x"}')
