"""Start development API against the dedicated local DB. OIDC is not implemented."""

import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "scripts"))
from runtime_env import apply_model_runtime_env, chat_provider_label

port = (
    subprocess.check_output(["docker", "port", "ringharness-development-pg", "5432"], text=True)
    .strip()
    .rsplit(":", 1)[1]
)
values = dict(
    line.split("=", 1) for line in (root / ".runtime/postgres.env").read_text().splitlines()
)
env = {
    **os.environ,
    "RING_DATABASE_URL": f"postgresql+psycopg://ring:{values['POSTGRES_PASSWORD']}@127.0.0.1:{port}/ring_test",
}
# 本机 Qwen 默认；若存在 .runtime/chat.env 则覆盖为 DeepSeek 等（不抢 OpenCode 的 :8001）
apply_model_runtime_env(root, env)
print(f"serve_local chat provider={chat_provider_label(env)}", flush=True)
subprocess.run(
    [
        "uv",
        "run",
        "uvicorn",
        "control_api.app:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        "58101",
    ],
    cwd=root,
    env=env,
    check=True,
)
