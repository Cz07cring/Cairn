"""``python -m workflow_worker``：Temporal Worker 入口。"""

from __future__ import annotations

import argparse
import logging
import sys

from workflow_worker.service import run_loop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ringharness workflow-worker（Python Temporal Worker；不写 Goal DONE）"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="加载配置并跑一轮后退出（单测 / 冒烟）",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        return run_loop(once=args.once)
    except Exception as exc:  # noqa: BLE001
        # 连接失败等统一失败关闭：短日志退出，不堆栈冒充「已编排」
        logging.getLogger("workflow_worker").error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
