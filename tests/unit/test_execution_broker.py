"""Broker 路径与输入协议的失败关闭边界。"""

from pathlib import Path

import pytest
from execution_broker import PathRejected, resolve_workspace_path


def test_resolve_rejects_traversal(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.py").write_text("x\n")
    with pytest.raises(PathRejected):
        resolve_workspace_path(tmp_path, "../etc/passwd", allowed_paths=["src/**"])
    with pytest.raises(PathRejected):
        resolve_workspace_path(tmp_path, "/etc/passwd", allowed_paths=["src/**"])
    with pytest.raises(PathRejected):
        resolve_workspace_path(tmp_path, "secret.txt", allowed_paths=["src/**"])


def test_parse_read_file_input_contract():
    from execution_broker.read_file import parse_read_file_input

    with pytest.raises(ValueError, match="合法 JSON"):
        parse_read_file_input(b"not-json")
    with pytest.raises(ValueError, match="path"):
        parse_read_file_input(b"{}")
    with pytest.raises(ValueError, match="非空"):
        parse_read_file_input(b'{"path":""}')
    # 非 ToolPayload / 非遗留 path 的 envelope 仍拒收
    with pytest.raises(ValueError, match="path"):
        parse_read_file_input(
            b'{"tool":"read_file","arguments":{"path":"a.ts"},"activity_id":"x"}'
        )
