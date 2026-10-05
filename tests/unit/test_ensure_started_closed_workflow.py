"""ensure_started 对「已关闭 Workflow」必须失败关闭，不得静默返回死 run_id。

**缺陷背景（本批修复）**：`RealTemporalClient._ensure_started_async` 在
`WorkflowAlreadyStartedError` 分支里**不查状态**就返回 run_id：

    if exc.run_id:
        return str(exc.run_id)          # ← 已关闭也照返回
    desc = await handle.describe()
    return str(desc.run_id)             # ← 同样不查状态

调用方 `relay.ensure_workflow` 随后**无条件** `acknowledge_delivery(...)`，
于是命令被标记为已投递、却指向一个**永远不会执行**的 run —— Goal 静默卡死，
从外部与「正常运行」无从区分。（本会话在同一文件里已修过两处同族静默失效：
模块 docstring 的 LEGACY 回退、以及 c28 的载荷白名单。）

另外两处同源问题一并修掉：

  - **进程内缓存盲信**：`self._runs.get(workflow_id)` 直接返回，无活性校验；
  - **谎报**：任何异常都被包成 `TemporalUnavailable("Temporal 不可用: …")`，
    把「已关闭（重试永远无用）」说成「暂时不可用（重试有意义）」。

本文件覆盖两件事：
  1. 纯判定函数 `_require_open_run` 对**全部 7 个** Temporal 状态穷举；
  2. 接线：`ensure_started` 经 monkeypatch 的 Client 走完整分支，且异常不被改写。
"""

from __future__ import annotations

import pytest
from orchestration.client import (
    RealTemporalClient,
    TemporalUnavailable,
    TemporalWorkflowClosed,
    _require_open_run,
    _status_name,
)
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError

# Temporal 的 7 个 Workflow 状态（穷举，避免将来新增状态被漏判）
_ALL_STATUSES = list(WorkflowExecutionStatus)


def test_status_enum_has_expected_members() -> None:
    """钉住状态集合：新增状态时应来改本文件，而不是被静默当成可复用。"""
    assert {s.name for s in _ALL_STATUSES} == {
        "RUNNING",
        "COMPLETED",
        "FAILED",
        "CANCELED",
        "TERMINATED",
        "CONTINUED_AS_NEW",
        "TIMED_OUT",
    }


@pytest.mark.parametrize("status", _ALL_STATUSES, ids=lambda s: s.name)
def test_require_open_run_only_reuses_running(status: WorkflowExecutionStatus) -> None:
    """**核心不变量**：只有 RUNNING 可复用 run_id，其余状态一律失败关闭。"""
    name = _status_name(status)
    if status is WorkflowExecutionStatus.RUNNING:
        assert _require_open_run("wf-1", run_id="run-1", status_name=name) == "run-1"
        return
    with pytest.raises(TemporalWorkflowClosed) as exc:
        _require_open_run("wf-1", run_id="run-1", status_name=name)
    # 异常须带上足够定位的信息（否则运维只看到「失败了」）
    assert exc.value.status_name == name
    assert exc.value.workflow_id == "wf-1"
    assert exc.value.run_id == "run-1"
    assert name in exc.value.message


def test_status_name_accepts_enum_and_plain_string() -> None:
    """状态归一：枚举取 .name，字符串原样（describe 返回枚举，测试桩可能给字符串）。"""
    assert _status_name(WorkflowExecutionStatus.RUNNING) == "RUNNING"
    assert _status_name("COMPLETED") == "COMPLETED"


class _FakeDesc:
    def __init__(self, run_id: str, status: WorkflowExecutionStatus) -> None:
        self.run_id = run_id
        self.status = status


class _FakeHandle:
    def __init__(self, desc: _FakeDesc) -> None:
        self._desc = desc

    async def describe(self) -> _FakeDesc:
        return self._desc


class _FakeTemporal:
    """模拟 temporalio Client：可选「已存在」与「已存在时的状态」。"""

    def __init__(
        self, *, already_started: bool, status: WorkflowExecutionStatus | None = None
    ) -> None:
        self._already_started = already_started
        self._desc = _FakeDesc("run-existing", status) if status is not None else None

    async def start_workflow(self, *_a: object, **_k: object) -> object:
        if self._already_started:
            raise WorkflowAlreadyStartedError(
                "wf-1", "GoalWorkflow", run_id="run-existing"
            )

        class _H:
            result_run_id = "run-fresh"

        return _H()

    def get_workflow_handle(self, _workflow_id: str) -> _FakeHandle:
        assert self._desc is not None
        return _FakeHandle(self._desc)


def _patch_connect(monkeypatch: pytest.MonkeyPatch, fake: _FakeTemporal) -> None:
    async def _fake_connect(*_a: object, **_k: object) -> _FakeTemporal:
        return fake

    monkeypatch.setattr(Client, "connect", staticmethod(_fake_connect))


def test_ensure_started_returns_fresh_run_when_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常启动：返回新 run_id。"""
    _patch_connect(monkeypatch, _FakeTemporal(already_started=False))
    client = RealTemporalClient(target="127.0.0.1:1")
    assert client.ensure_started("wf-1") == "run-fresh"


def test_ensure_started_reuses_run_id_while_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """幂等正例：已存在且**在跑** → 复用其 run_id（这才是真幂等）。"""
    _patch_connect(
        monkeypatch,
        _FakeTemporal(already_started=True, status=WorkflowExecutionStatus.RUNNING),
    )
    client = RealTemporalClient(target="127.0.0.1:1")
    assert client.ensure_started("wf-1") == "run-existing"


@pytest.mark.parametrize(
    "status",
    [s for s in _ALL_STATUSES if s is not WorkflowExecutionStatus.RUNNING],
    ids=lambda s: s.name,
)
def test_ensure_started_raises_closed_not_unavailable(
    monkeypatch: pytest.MonkeyPatch, status: WorkflowExecutionStatus
) -> None:
    """已关闭时必须抛 TemporalWorkflowClosed，**且不得**被改写成 TemporalUnavailable。

    改写会让调用方把「重试永远无用」误读为「暂时不可用、稍后重试」。
    """
    _patch_connect(monkeypatch, _FakeTemporal(already_started=True, status=status))
    client = RealTemporalClient(target="127.0.0.1:1")
    with pytest.raises(TemporalWorkflowClosed) as exc:
        client.ensure_started("wf-1")
    assert exc.value.status_name == status.name
    # 关键：不能是「不可用」
    assert not isinstance(exc.value, TemporalUnavailable)


def test_process_cache_is_not_trusted_for_short_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缓存**不得**做短路返回。

    否则长驻进程会把已关闭的 run_id 反复交回调用方（静默投递到死 run）。
    做法：启动一次拿到 fresh run，随后让「服务端」变为已关闭，
    再次调用必须抛 TemporalWorkflowClosed —— 若缓存被信任则会返回旧的 run-fresh。
    """
    fake = _FakeTemporal(already_started=False)
    _patch_connect(monkeypatch, fake)
    client = RealTemporalClient(target="127.0.0.1:1")
    assert client.ensure_started("wf-1") == "run-fresh"

    # 服务端此后报告已关闭
    fake._already_started = True
    fake._desc = _FakeDesc("run-existing", WorkflowExecutionStatus.COMPLETED)

    with pytest.raises(TemporalWorkflowClosed):
        client.ensure_started("wf-1")
