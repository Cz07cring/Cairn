"""Run against the explicitly named, disposable local ringharness DB only."""

import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "scripts"))
from runtime_env import apply_model_runtime_env

port = (
    subprocess.check_output(["docker", "port", "ringharness-development-pg", "5432"], text=True)
    .strip()
    .rsplit(":", 1)[1]
)
values = dict(
    line.split("=", 1) for line in (root / ".runtime/postgres.env").read_text().splitlines()
)
url = f"postgresql+psycopg://ring:{values['POSTGRES_PASSWORD']}@127.0.0.1:{port}/ring_test"
env = {**os.environ, "RING_DATABASE_URL": url, "RING_TEST_DATABASE_URL": url}
s3_port = (
    subprocess.check_output(["docker", "port", "ringharness-development-s3", "9000"], text=True)
    .strip()
    .rsplit(":", 1)[1]
)
s3_values = dict(
    line.split("=", 1) for line in (root / ".runtime/minio.env").read_text().splitlines()
)
env.update(
    {
        "RING_TEST_S3_ENDPOINT": f"http://127.0.0.1:{s3_port}",
        "RING_TEST_S3_ACCESS_KEY": s3_values["MINIO_ROOT_USER"],
        "RING_TEST_S3_SECRET_KEY": s3_values["MINIO_ROOT_PASSWORD"],
    }
)
apply_model_runtime_env(root, env)
subprocess.run(["uv", "run", "alembic", "upgrade", "head"], cwd=root, env=env, check=True)
subprocess.run(["uv", "run", "pytest", "-q"], cwd=root, env=env, check=True)
