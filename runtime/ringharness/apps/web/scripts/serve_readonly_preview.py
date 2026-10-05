"""在本机以只读 viewer 身份展示一个真实活动 Goal。"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import jwt
import uvicorn
from control_api.app import create_app
from control_api.settings import Settings
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "apps" / "web"


def select_goal(database_url: str) -> tuple[str, str]:
    requested = os.environ.get("RING_WEB_PREVIEW_GOAL_ID", "").strip()
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as db:
            if requested:
                row = db.execute(
                    text("SELECT project_id, id FROM goals WHERE id=:id"),
                    {"id": UUID(requested)},
                ).one_or_none()
            else:
                row = db.execute(
                    text(
                        """
                        SELECT g.project_id, g.id
                        FROM goals g
                        WHERE EXISTS (SELECT 1 FROM tasks t WHERE t.goal_id=g.id)
                          AND g.status IN ('RUNNING','VERIFYING','PLANNING','BLOCKED','PAUSED')
                        ORDER BY CASE g.status
                          WHEN 'RUNNING' THEN 0 WHEN 'VERIFYING' THEN 1
                          WHEN 'PLANNING' THEN 2 WHEN 'BLOCKED' THEN 3 ELSE 4 END,
                          g.created_at DESC
                        LIMIT 1
                        """
                    )
                ).one_or_none()
    finally:
        engine.dispose()
    if row is None:
        raise SystemExit("开发库中没有带 Task 的活动 Goal")
    return str(row[0]), str(row[1])


def main() -> None:
    database_url = os.environ.get("RING_WEB_PREVIEW_DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("须设置 RING_WEB_PREVIEW_DATABASE_URL")
    project_id, goal_id = select_goal(database_url)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    now = datetime.now(UTC)
    bearer = jwt.encode(
        {
            "sub": "web-readonly-preview",
            "roles": ["viewer"],
            "project_ids": [project_id],
            "iss": "ring-web-preview",
            "aud": "ring-api",
            "iat": now,
            # API 强制 JWT 生命周期 ≤ 1 小时；预览服务重启会同时轮换签名键。
            "exp": now + timedelta(minutes=50),
        },
        private,
        algorithm="RS256",
    )
    api_port = int(os.environ.get("RING_WEB_PREVIEW_API_PORT", "58121"))
    web_port = int(os.environ.get("RING_WEB_PREVIEW_WEB_PORT", "58104"))
    settings = Settings(
        database_url=database_url,
        jwt_public_key=public,
        jwt_issuer="ring-web-preview",
        jwt_audience="ring-api",
        cursor_secret="readonly-preview-cursor-secret-at-least-32-bytes",
        repository_refs=["fixture"],
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=api_port, log_level="warning")
    )
    api_thread = threading.Thread(target=server.run, daemon=True)
    api_thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise SystemExit("只读 Control API 启动失败")

    env = {
        **os.environ,
        "VITE_RING_CONTROL_URL": f"http://127.0.0.1:{api_port}",
        "VITE_RING_DEV_BEARER": f"Bearer {bearer}",
        "VITE_RING_PROJECT_ID": project_id,
        "VITE_RING_GOAL_ID": goal_id,
    }
    vite = subprocess.Popen(
        ["pnpm", "dev", "--port", str(web_port), "--strictPort"],
        cwd=WEB,
        env=env,
    )

    def stop(_signum: int, _frame: object) -> None:
        vite.terminate()
        server.should_exit = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(f"只读驾驶舱：http://127.0.0.1:{web_port}/#run-overview")
    print(f"观察 Goal：{goal_id}（viewer，仅可读取）")
    try:
        vite.wait()
    finally:
        server.should_exit = True
        api_thread.join(timeout=5)


if __name__ == "__main__":
    main()
