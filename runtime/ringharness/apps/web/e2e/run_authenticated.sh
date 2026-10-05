#!/usr/bin/env bash
# 认证 Web E2E：只接受显式隔离库，先迁移再启动短期身份/API/浏览器。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
: "${RING_WEB_E2E_DATABASE_URL:?须设置为独立 Web E2E PostgreSQL 数据库，禁止复用生产或共享业务库}"

cd "$ROOT"
RING_DATABASE_URL="$RING_WEB_E2E_DATABASE_URL" uv run alembic upgrade head
cd apps/web
pnpm exec playwright test --config playwright.auth.config.ts
