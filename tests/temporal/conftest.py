"""TM 专项：复用 integration 的真实 PG HTTP `api` fixture。"""

from __future__ import annotations

import sys
from pathlib import Path

# test_claims / test_orchestration_backend 以同目录模块名导入
_INTEGRATION = Path(__file__).resolve().parents[1] / "integration"
_path = str(_INTEGRATION)
if _path not in sys.path:
    sys.path.insert(0, _path)

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "_ring_integration_conftest",
    _INTEGRATION / "conftest.py",
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# 注册 integration 的 api fixture（真实 TestClient + JWT）
api = _mod.api
