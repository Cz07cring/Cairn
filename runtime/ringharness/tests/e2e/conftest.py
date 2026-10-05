"""E2E 目录：复用 integration 的真实 PG `api` / 根 `objects` fixture。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_INTEGRATION = Path(__file__).resolve().parents[1] / "integration"
_path = str(_INTEGRATION)
if _path not in sys.path:
    sys.path.insert(0, _path)

_spec = importlib.util.spec_from_file_location(
    "_ring_e2e_integration_conftest",
    _INTEGRATION / "conftest.py",
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

api = _mod.api
