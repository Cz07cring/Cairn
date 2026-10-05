"""``python -m execution_broker``：长驻入口（非调度权威）。"""

from __future__ import annotations

import argparse
import logging
import sys

from execution_broker.service import run_loop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ringharness ExecutionBroker（可信执行宿主，非 Goal/Task 调度权威）"
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
    except RuntimeError as exc:
        # 业务库凭据等失败关闭：短日志退出，不堆栈冒充「可调度」
        logging.getLogger("execution_broker").error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
