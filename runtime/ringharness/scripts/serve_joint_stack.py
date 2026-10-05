#!/usr/bin/env python3
"""本机联调栈启动器（control / workflow-worker / runner / broker）。

存在理由：此前把「能跑起来」这套编排写在了 gitignore 的 `.runtime/joint/*.sh` 里，
于是它与仓库自己的权威语义漂移，并且无法评审、无法复现。已实测到的三处漂移：

1. **模型环境优先级反转**：`.runtime/joint/env.sh` 先 source chat.env 再 source qwen.env，
   而 shell 的 `.` 是普通赋值 ⇒ qwen.env 反向压过 chat.env。结果是 base/key 指向 DeepSeek
   却带着本机 Qwen 的 model 名 → `model not found` → 模型不产出工具调用 → 活动
   FAILED(NO_TOOL_PROPOSAL) → Goal 永久停在 RUNNING。仓库权威语义见 `scripts/runtime_env.py`
   （显式 export > chat.env > qwen.env），本启动器改为直接调用它，不再手写 source 顺序。
2. **runner 缺 `RING_RUNNER_WORKER_JWT`**：`start_stack.sh` 未传入，RunActivation 只能
   报「缺少 RING_RUNNER_WORKER_JWT」而无法真实执行。
3. **未跑模型目录闸门**：`scripts/chat_model_gate.py` 早已实现「model 不在 /v1/models 即
   失败关闭」，但只接在测试里，启动器一律绕过 ⇒ 错配可以静默空转。

本启动器只做编排与失败关闭，不写任何业务状态；≠ Goal DONE。

用法::

    uv run python scripts/serve_joint_stack.py --start     # 起栈并健康检查
    uv run python scripts/serve_joint_stack.py --stop      # 停栈
    uv run python scripts/serve_joint_stack.py --status    # 看状态
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from runtime_env import apply_model_runtime_env, chat_provider_label

JOINT = ROOT / ".runtime" / "joint"
CONTROL_PORT = 58101
CONTROL_URL = f"http://127.0.0.1:{CONTROL_PORT}"

# runner/broker 出示的 worker 主体，**必须**等于 workflow-worker 领取活动时用的主体
# （见 kernel_activities.py 的 RING_WORKFLOW_WORKER_SUBJECT），故下方 worklow-worker 进程
# 显式设同一值。控制面公开读路由按「是否持有该活动」判可见性，主体不一致时 runner 读自己
# 的活动得 404 NOT_FOUND → ACTIVITY_GET_FAILED → PLAN 反复失败。
#
# 用**联调专属**主体而非默认的 ring-workflow-worker：失败的 activation 会按设计把资源预留
# 留在 HELD 等对账（record_activation_termination 不释放资源），而默认主体与其它实验共用，
# 残留会把 model_slots 吃满，之后一切准入都报「Worker 资源不足以准入该活动」。
WORKER_SUBJECT = "joint-arb-worker"


def log(msg: str) -> None:
    print(f"[serve_joint_stack] {msg}", flush=True)


def fail(msg: str) -> NoReturn:
    print(f"[serve_joint_stack] 失败关闭: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(2)


# --------------------------------------------------------------------------- 环境


def build_env() -> dict[str, str]:
    """装配栈环境。模型相关键走 runtime_env 的权威合并，不手写 source 顺序。

    **先剥离继承来的模型/云相关键**：本启动器拥有整栈配置，而 `.runtime/*.env` 才是权威。
    实测踩坑：调用者 shell 里残留的 `RING_LOCAL_QWEN_MODEL`（早期 source 留下的旧值）
    会被 runtime_env 视作「进程显式 export」而压过 chat.env，导致
      · runner 拿旧模型名、ModelProfile 却是新模型 → 登记 422，
      · 或两边都旧 → 推理吃光 512 token → 空正文 → 活动失败、Goal 永停 PLANNING。
    需要临时覆盖时显式设 ``RING_JOINT_ALLOW_ENV_OVERRIDE=1``。
    """
    env = dict(os.environ)
    if (env.get("RING_JOINT_ALLOW_ENV_OVERRIDE") or "").strip() not in {"1", "true", "yes"}:
        for key in list(env):
            if key.startswith(("RING_LOCAL_QWEN_", "RING_CHAT_", "RING_CLOUD_MODE")):
                env.pop(key, None)

    postgres = _read_env_file(ROOT / ".runtime" / "postgres.env")
    minio = _read_env_file(ROOT / ".runtime" / "minio.env")
    if not postgres or not minio:
        fail("缺少 .runtime/postgres.env 或 .runtime/minio.env（先跑 scripts/test_local.py 的准备步骤）")

    pg_port = _docker_port("ringharness-development-pg", 5432)
    s3_port = _docker_port("ringharness-development-s3", 9000)
    if pg_port is None or s3_port is None:
        fail("开发容器未就绪：需要 ringharness-development-pg / -s3 在运行")

    # 模型环境：qwen.env 做默认，chat.env 覆盖，进程显式 export 最高（与 serve_local 同语义）
    apply_model_runtime_env(ROOT, env)

    joint_public = JOINT / "jwt_public.pem"
    if not joint_public.is_file():
        fail(f"缺少联调 JWT 公钥 {joint_public}；先跑本脚本 --start 的建钥步骤")

    env.update(
        {
            "RING_DATABASE_URL": (
                f"postgresql+psycopg://{postgres['POSTGRES_USER']}:"
                f"{postgres['POSTGRES_PASSWORD']}@127.0.0.1:{pg_port}/ring_test"
            ),
            "RING_JWT_PUBLIC_KEY": joint_public.read_text(encoding="utf-8"),
            "RING_JWT_ISSUER": "ring-joint",
            "RING_JWT_AUDIENCE": "ring-api",
            "RING_CURSOR_SECRET": "joint-debug-cursor-secret-at-least-32-bytes",
            "RING_REPOSITORY_REFS": '["fixture","joint-arb"]',
            "RING_REPOSITORY_PATHS": json.dumps({"joint-arb": str(JOINT / "arb_workspace")}),
            "RING_S3_ENDPOINT": f"http://127.0.0.1:{s3_port}",
            "RING_S3_BUCKET": minio.get("MINIO_BUCKET", "ring"),
            "RING_S3_ACCESS_KEY": minio["MINIO_ROOT_USER"],
            "RING_S3_SECRET_KEY": minio["MINIO_ROOT_PASSWORD"],
            "RING_TEMPORAL_TARGET": "127.0.0.1:7233",
            "RING_TEMPORAL_NAMESPACE": "default",
            "RING_CONTROL_URL": CONTROL_URL,
            "RING_RUNNER_CONTROL_URL": CONTROL_URL,
        }
    )
    env.setdefault("RING_TEST_DATABASE_URL", env["RING_DATABASE_URL"])
    env.setdefault("RING_CLOUD_MODE", "PREAUTHORIZED")
    env.setdefault(
        "RING_HARNESS_CHECKOUT",
        str(ROOT / ".runtime" / "harness-pins" / "c291e7961a515f6d7af9304e7fd1d257929aef26"),
    )
    return env


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip()
    return out


def _docker_port(container: str, port: int) -> int | None:
    try:
        raw = subprocess.check_output(["docker", "port", container, str(port)], text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    if not raw:
        return None
    return int(raw.splitlines()[0].rsplit(":", 1)[1])


# --------------------------------------------------------------------------- 闸门


def assert_model_aligned(env: dict[str, str]) -> None:
    """模型目录闸门：model 不在端点 /v1/models 即失败关闭。

    这是本次「跑不起来」的直接防线：错配时宁可起不来，也不要静默空转成
    `NO_TOOL_PROPOSAL` 再让 Goal 永久停在 RUNNING。
    """
    from chat_model_gate import ChatModelMismatch, assert_chat_model_configured

    try:
        result = assert_chat_model_configured(env, timeout=15.0)
    except ChatModelMismatch as exc:
        fail(
            f"chat 模型与端点目录不一致：{exc}\n"
            f"        当前 provider={chat_provider_label(env)} "
            f"base={env.get('RING_LOCAL_QWEN_BASE')} model={env.get('RING_LOCAL_QWEN_MODEL')}\n"
            "        修法：对齐 .runtime/chat.env 与进程 RING_LOCAL_QWEN_*"
        )
    log(f"模型闸门通过 provider={chat_provider_label(env)} model={result.model}")


# --------------------------------------------------------------------------- JWT


def mint_tokens(env: dict[str, str]) -> str:
    """铸造联调 JWT 并登记 worker；返回 worker bearer（裸 token，不带 Bearer 前缀）。"""
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from sqlalchemy import create_engine, text

    private = serialization.load_pem_private_key(
        (JOINT / "jwt_private.pem").read_bytes(), password=None
    )
    assert isinstance(private, rsa.RSAPrivateKey), "联调私钥须为 RSA"
    now = datetime.now(UTC)

    def mint(roles: list[str], sub: str) -> str:
        return jwt.encode(
            {
                "sub": sub,
                "roles": roles,
                "project_ids": [],
                "iss": "ring-joint",
                "aud": "ring-api",
                "iat": now,
                # 硬上限 1 小时：apps/control 的 identity 显式拒绝 exp-iat > 3600
                # （`raise ValueError("token lifetime")` → 401），
                # 与本仓 mint_identity.py 的 50 分钟一致。凭此令牌的长跑需中途重铸。
                "exp": now + timedelta(minutes=50),
            },
            private,
            algorithm="RS256",
        )

    operator = mint(["admin", "operator", "viewer"], "joint-operator")
    worker = mint(["worker"], WORKER_SUBJECT)
    (JOINT / "operator.bearer").write_text(f"Bearer {operator}\n", encoding="utf-8")
    (JOINT / "worker.bearer").write_text(worker, encoding="utf-8")

    kinds = ["PLAN", "EXECUTE", "AUDIT", "INTEGRATE", "FINALIZE", "RECONCILE"]
    engine = create_engine(env["RING_DATABASE_URL"])
    try:
        with engine.begin() as db:
            for subject in (WORKER_SUBJECT,):
                row = (
                    db.execute(
                        text("SELECT id FROM workers WHERE subject=:s AND status='ACTIVE'"),
                        {"s": subject},
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    db.execute(
                        text(
                            """INSERT INTO workers(
                              id,subject,allowed_kinds,capabilities,cpu_millicores,memory_bytes,
                              disk_bytes,model_slots,browser_slots,status)
                            VALUES(:id,:subject,:kinds,'{}',4000,:mem,:disk,8,0,'ACTIVE')"""
                        ),
                        {
                            "id": str(uuid4()),
                            "subject": subject,
                            "kinds": kinds,
                            "mem": 8 * 2**30,
                            "disk": 8 * 2**30,
                        },
                    )
    finally:
        engine.dispose()
    log("已铸造 operator / worker JWT（50 分钟，受 control 的 1 小时上限约束）并登记 worker")
    return worker


# --------------------------------------------------------------------------- 进程


def _pid_file(name: str) -> Path:
    return JOINT / f"{name}.pid"


def stop_stack() -> None:
    for name in ("control", "workflow-worker", "runner", "broker"):
        pf = _pid_file(name)
        if not pf.is_file():
            continue
        try:
            pid = int(pf.read_text().strip())
        except ValueError:
            pf.unlink(missing_ok=True)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            log(f"已停 {name} (pid={pid})")
        except ProcessLookupError:
            log(f"{name} 已不在 (pid={pid})")
        pf.unlink(missing_ok=True)
    time.sleep(1.5)


def _spawn(name: str, args: list[str], env: dict[str, str], cwd: Path) -> None:
    logf = (JOINT / f"{name}.log").open("ab", buffering=0)
    proc = subprocess.Popen(
        args, cwd=str(cwd), env=env, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True
    )
    _pid_file(name).write_text(str(proc.pid), encoding="utf-8")
    log(f"已起 {name} (pid={proc.pid}) → .runtime/joint/{name}.log")


def start_stack(env: dict[str, str]) -> None:
    JOINT.mkdir(parents=True, exist_ok=True)
    (JOINT / "arb_workspace").mkdir(exist_ok=True)
    _ensure_jwt_keys()
    _wait_control_gone()
    stop_stack()

    log("alembic upgrade head …")
    subprocess.run(["uv", "run", "alembic", "upgrade", "head"], cwd=str(ROOT), env=env, check=True)

    worker_jwt = mint_tokens(env)

    # control：文件内 PEM 启动（避免环境变量折断换行），见 .runtime/joint/run_control.py
    run_control = JOINT / "run_control.py"
    if not run_control.is_file():
        fail(f"缺少 {run_control}")
    _spawn("control", ["uv", "run", "python", str(run_control)], env, ROOT)
    _wait_health()

    # workflow-worker：持业务库供 Kernel Activities；须以联调专属主体 claim，
    # 否则默认主体与 runner 出示的身份不一致（见 WORKER_SUBJECT 注释）。
    _spawn(
        "workflow-worker",
        ["uv", "run", "python", "-m", "workflow_worker"],
        {**env, "RING_WORKFLOW_WORKER_SUBJECT": WORKER_SUBJECT},
        ROOT,
    )

    # runner：Temporal 侧 RunActivation；必须带 worker JWT，否则只能报缺凭据
    _spawn("runner", ["pnpm", "--filter", "@ring/runner", "temporal-worker"], _runner_env(env, worker_jwt), ROOT)

    _spawn("broker", ["bash", str(_broker_supervisor(worker_jwt))], _broker_env(env, worker_jwt), ROOT)

    log(f"栈已起：control={CONTROL_URL} temporal=127.0.0.1:7233（日志见 .runtime/joint/*.log）")


def _runner_env(env: dict[str, str], worker_jwt: str) -> dict[str, str]:
    """runner 进程环境。

    两个开关缺一不可，缺任一个都会「静默降级」而不是失败关闭：
    - 缺 LIVE_DISPATCH：liveDispatch=false，PLAN 走夹具分支，正文 `fixture-plan-host`，
      LivePlanParseError，耗时约 128ms（真实模型调用不可能这么快）。实测代价：Goal 停在
      PLANNING，且模型调用表显示 AUTHORIZED 而非 DISPATCHED。
    - 缺 EXECUTE_RUNTIME：执行腿走脚本化路径，不是真实多轮工具循环。
    """
    return {
        **env,
        "RING_RUNNER_WORKER_JWT": worker_jwt,
        "RING_WORKER_JWT": worker_jwt,
        # **按激活重读**：控制面对 exp-iat 有 1 小时硬上限，Runner 无法持长期凭据。
        # 指向续铸文件后，broker 托管循环里的 --refresh-bearer 一并让 Runner 自愈。
        "RING_RUNNER_WORKER_JWT_FILE": str(JOINT / "worker.bearer"),
        "RING_RUNNER_BUILD_ID": "m0-ts-1.23.0-dev",
        # doc/engineering/Harness接入现状与缺口-核对-2026-09-12.md 曾记为「未设」
        "RING_RUNNER_LIVE_DISPATCH": "1",
        "RING_HARNESS_EXECUTE_RUNTIME": "deepseek-official-agent-loop",
        # **执行模式必须显式设为 diagnose**。缺此项时 `resolveOfficialExecuteMode` 回退
        # `read_file` 分支（只读一回合就返回 ACTIVATION_SUBMITTED），活动停在 RUNNING、
        # 工作流只能在观察窗口里空轮询——从外部看像「卡住」，实则是配置默认值选错了分支。
        # diagnose = 官方 chat 驱动循环：read_file → run_tests → write_file → run_tests
        # → seal_candidate（封印 profile 由 Goal/Task 合同给出）。
        "RING_HARNESS_EXECUTE_OFFICIAL_MODE": "diagnose",
        # diagnose 分支**确实读取**这两项（与 read_file 分支不同）：readPath 是诊断目标，
        # writePath 是修复落点。指到工作区里带缺陷的价差模块。
        "RING_HARNESS_EXECUTE_READ_PATH": "arb/spread.py",
        "RING_HARNESS_EXECUTE_WRITE_PATH": "arb/spread.py",
        # 心跳 5s 与 e2e4（已实证到 DONE）一致；间隔须满足「TTL ≥ 3×心跳」。
        "RING_HARNESS_EXECUTE_HEARTBEAT_MS": "5000",
        "RING_HARNESS_EXECUTE_NEXT_RENEWAL_SEQ": "1",
        # **必须显式给提示词**。缺省提示词只写「诊断并修复 <path>」，而官方循环的工具集里
        # **没有目录列举能力**（只有 read_file / run_tests / write_file），模型只能靠**猜路径**
        # 探索 —— 实测 39 次失败的 read_file（market/specs.json、multipliers.json …）而从不猜中
        # 真实文件名，活动停在 RUNNING 什么也没产出。给出确切的文件清单与步骤顺序即可闭环。
        "RING_HARNESS_EXECUTE_USER_PROMPT": (
            "诊断并修复 arb/spread.py。工作区任务合同在 TASK.md；禁止探测未列出的路径。\n"
            "严格按序执行，每步都要真实调用工具：\n"
            "1) read_file TASK.md（公开函数合同，参数名与计算口径必须保持一致）\n"
            "2) read_file arb/spread.py（当前实现，含缺陷）\n"
            "3) read_file tests/test_spread.py（公开期望行为）\n"
            "4) read_file market/contracts.json（两个合约的真实规格）\n"
            "5) write_file arb/spread.py，把两处缺陷改对：\n"
            "   a. spread_bps 须把价差按参考价归一化到 bps：(ask-bid)/ask*10000；\n"
            "   b. cross_contract_bps 在未给面值/计价单位时必须 raise ValueError，\n"
            "      不得把不同计价单位的合约价格直接相减。\n"
            "6) run_tests，参数 suite 必须是 public（值只能是 public 或 auditor）。\n"
            "7) 若第 6 步不是全绿，回到第 5 步继续修，直到 run_tests 全绿。\n"
            "8) seal_candidate\n"
            "【硬性约束】Kernel 规定：write_file 之后必须存在一次 exit_code=0 的 run_tests，"
            "否则 seal_candidate 会被直接拒绝（报错 SEAL_REQUIRES_GREEN_TESTS）。"
            "请勿跳过 run_tests 直接封印，也不要反复重试被拒的 seal_candidate。\n"
            "禁止真实下单。"
        ),
        # **输出预算要按上下文长度放大**。推理模型的 reasoning token 随输入增长而增长：
        # 单轮小提示词下 2048 够（实测 0/3 空），但执行腿读到 3 个文件后上下文变长，
        # 推理会吃掉更大比例，2048 仍可能把 tool_call 截断（finish_reason=length 而模型
        # 表现为「读完就不提工具」）。执行腿读 3 个文件 + 长提示词，故取 8192。
        "RING_MODEL_MIN_OUTPUT_TOKENS": "8192",
    }


def _broker_env(env: dict[str, str], worker_jwt: str) -> dict[str, str]:
    """broker 进程环境：持 worker JWT 与控制面地址。

    **不得**持业务库凭据（AGENTS 依赖边界：Broker 无业务库写权限）。故先剔除所有
    ``RING_DATABASE*`` / ``RING_TEST_DATABASE_URL``，再注入 RING_BROKER_*。
    """
    out = {
        k: v
        for k, v in env.items()
        if not k.startswith(("RING_DATABASE", "RING_TEST_DATABASE", "RING_CONTROL_DATABASE"))
    }
    out.update(
        {
            "RING_BROKER_CONTROL_URL": CONTROL_URL,
            "RING_BROKER_WORKER_JWT": worker_jwt,
            "RING_BROKER_WORKSPACE_ROOT": str(JOINT / "arb_workspace"),
            "RING_BROKER_ALLOWED_PATHS": "*",
        }
    )
    return out


def _broker_supervisor(token: str) -> Path:
    """生成 broker 托管循环。

    broker 遇到 401/网络抖动会直接退出，而它退出**没有任何人知道** —— 工具链断掉后
    Goal 只会静静停在 RUNNING（实测该状态持续 14 小时无人察觉）。故用循环托管：
    每轮重读 worker.bearer，重铸令牌后可自愈；退出原因写进 broker.log。
    """
    script = JOINT / "broker_supervisor.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "# 由 scripts/serve_joint_stack.py 生成；联调 broker 托管循环\n"
        "set -uo pipefail\n"
        f"cd {ROOT}\n"
        f'export RING_BROKER_CONTROL_URL="{CONTROL_URL}"\n'
        f'export RING_BROKER_WORKER_JWT="{token}"\n'
        "while true; do\n"
        # **每轮先续铸令牌**：控制面对 exp-iat 有 1 小时硬上限，长跑无法持长期凭据。
        # 此前只重读 worker.bearer 而无人刷新它 ⇒ 令牌到期后 broker 401 退出、循环
        # 用同一张过期令牌重启，形成永不恢复的 401 空转（实测：broker.log 反复
        # 「拉取可派发 effect 失败：HTTP 401 登录凭据无效或已过期」rc=2）。
        f"  uv run python {ROOT}/scripts/serve_joint_stack.py --refresh-bearer "
        ">/dev/null 2>&1 || true\n"
        "  if [ -s .runtime/joint/worker.bearer ]; then\n"
        "    export RING_BROKER_WORKER_JWT=\"$(cat .runtime/joint/worker.bearer)\"\n"
        "  fi\n"
        "  uv run python -m execution_broker\n"
        "  rc=$?\n"
        "  echo \"$(date -u +%FT%TZ) broker-exit rc=$rc; 1s 后重启（托管循环）\"\n"
        "  sleep 1\n"
        "done\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _ensure_jwt_keys() -> None:
    if (JOINT / "jwt_private.pem").is_file() and (JOINT / "jwt_public.pem").is_file():
        return
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    JOINT.mkdir(parents=True, exist_ok=True)
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (JOINT / "jwt_private.pem").write_bytes(
        priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (JOINT / "jwt_public.pem").write_bytes(
        priv.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    log("已生成联调 JWT 密钥对")


def _wait_health(timeout_s: float = 45.0) -> None:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{CONTROL_URL}/health/live", timeout=2) as r:
                if r.status == 200:
                    log("control 健康检查通过")
                    return
        except (TimeoutError, urllib.error.URLError, OSError):
            time.sleep(0.5)
    fail("control 健康检查超时（见 .runtime/joint/control.log）")


def _wait_control_gone(timeout_s: float = 10.0) -> None:
    import socket

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", CONTROL_PORT)) != 0:
                return
        time.sleep(0.5)


def status() -> None:
    for name in ("control", "workflow-worker", "runner", "broker"):
        pf = _pid_file(name)
        if not pf.is_file():
            print(f"  {name:<16} 未记录")
            continue
        try:
            pid = int(pf.read_text().strip())
            os.kill(pid, 0)
            print(f"  {name:<16} 在跑 (pid={pid})")
        except (ValueError, ProcessLookupError):
            print(f"  {name:<16} 已死 (pid 文件残留)")


def main() -> int:
    ap = argparse.ArgumentParser(description="本机联调栈启动器")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--start", action="store_true")
    g.add_argument("--stop", action="store_true")
    g.add_argument("--status", action="store_true")
    # 令牌重铸：供长跑组件（runner / broker 托管循环）在每次重启时取新凭据。
    # 控制面对 exp-iat 有 1 小时硬上限，令牌无法铸得更久；长跑只能靠**续铸 + 重启自愈**。
    g.add_argument("--refresh-bearer", action="store_true")
    ap.add_argument("--skip-model-gate", action="store_true", help="调试用；正式起栈不应跳过")
    args = ap.parse_args()

    if args.status:
        status()
        return 0
    if args.stop:
        stop_stack()
        return 0
    if args.refresh_bearer:
        # 幂等：只重写 bearer 文件并补登 worker，不动任何进程
        mint_tokens(build_env())
        print("[serve_joint_stack] 已重铸 worker/operator bearer（50 分钟有效）")
        return 0

    if not JOINT.is_dir():
        fail(f"缺少联调目录 {JOINT}（含 run_control.py 等）")
    env = build_env()
    if not args.skip_model_gate:
        assert_model_aligned(env)
    start_stack(env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
