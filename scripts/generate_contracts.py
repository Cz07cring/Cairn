"""Generate implemented API contracts only; absent routes stay explicit in coverage report."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from control_api.app import create_app
from control_api.settings import Settings
from control_kernel.protocols.goals import GoalCreate

root = Path(__file__).resolve().parents[1]
out = root / "contracts"
out.mkdir(exist_ok=True)
for name in (
    "content-v3.schema.json",
    "event-v1.schema.json",
    "canonicalization-v3.json",
    "hash-vectors-v3.json",
):
    (out / name).write_bytes((root / "doc/contracts" / name).read_bytes())
(out / "goal-create.schema.json").write_text(
    json.dumps(GoalCreate.model_json_schema(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
)
app = create_app(Settings())
schema = app.openapi()
(out / "openapi.json").write_text(
    json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
)
implemented = sorted(
    f"{method.upper()} {path}"
    for path, methods in schema["paths"].items()
    for method in methods
    if method in ("get", "post", "put", "delete", "patch")
)
api = (root / "doc/05-API接口文档.md").read_text()
public = api.split("## 5. 公共路由")[1].split("## 6.")[0]
planned = sorted(
    f"{method} /api/v1{path}"
    for method, path in re.findall(r"\| (GET|POST|PUT|PATCH|DELETE) (/[^ |]+)", public)
)
coverage = {
    "status": "PARTIAL_IMPLEMENTATION",
    "implemented": implemented,
    "pending_public_routes": [p for p in planned if p not in implemented],
    "note": "Readiness covers project/config API only. Internal runtime and U02 routes are not implemented.",
}
(out / "implementation-coverage.json").write_text(
    json.dumps(coverage, ensure_ascii=False, indent=2) + "\n"
)
subprocess.run(
    [
        "pnpm",
        "exec",
        "openapi-typescript",
        str(out / "openapi.json"),
        "-o",
        str(root / "packages/api-client/src/generated.ts"),
    ],
    cwd=root,
    check=True,
)
manifest = {"spec": "v0.5", "implementation": "0.1.0-partial", "sources": {}, "artifacts": {}}
for p in [
    root / "doc/contracts" / n
    for n in (
        "content-v3.schema.json",
        "event-v1.schema.json",
        "canonicalization-v3.json",
        "hash-vectors-v3.json",
    )
]:
    manifest["sources"][str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
source_paths = (
    list((root / "apps/control/src").rglob("*.py"))
    + list((root / "packages/control_kernel/src/control_kernel/protocols").rglob("*.py"))
    + [root / "scripts/generate_contracts.py", root / "uv.lock", root / "pnpm-lock.yaml"]
)
for p in sorted(source_paths):
    manifest["sources"][str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
for p in sorted(out.glob("*.json")):
    if p.name != "generation-manifest.json":
        manifest["artifacts"][str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
p = root / "packages/api-client/src/generated.ts"
manifest["artifacts"][str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
(out / "generation-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
