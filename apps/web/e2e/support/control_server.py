"""为 Web E2E 启动隔离 Control API；密钥只从父进程短期注入。"""

from __future__ import annotations

import base64
import os

import uvicorn
from control_api.app import create_app
from control_api.settings import Settings


def main() -> None:
    database_url = os.environ.get("RING_WEB_E2E_DATABASE_URL", "").strip()
    public_key_b64 = os.environ.get("RING_WEB_E2E_JWT_PUBLIC_KEY_B64", "").strip()
    if not database_url or not public_key_b64:
        raise SystemExit("缺少隔离数据库或短期 E2E 公钥")
    public_key = base64.b64decode(public_key_b64, validate=True).decode("utf-8")
    settings = Settings(
        database_url=database_url,
        jwt_public_key=public_key,
        jwt_issuer="ring-web-e2e",
        jwt_audience="ring-api",
        cursor_secret="web-e2e-cursor-secret-at-least-32-bytes",
        repository_refs=["fixture"],
        session_secret="web-e2e-session-secret-at-least-32-bytes",
        auth_return_to_origins=["http://127.0.0.1:58114"],
    )
    uvicorn.run(
        create_app(settings),
        host="127.0.0.1",
        port=int(os.environ.get("RING_WEB_E2E_API_PORT", "58121")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
