import {describe, expect, it} from 'vitest';
import {assessRunProgress, buildRunLiveTimeline} from './runLiveTimelineView.js';

describe('buildRunLiveTimeline', () => {
  it('把模型、步骤、命令和工具结果按时间合成普通人可读的运行实况', () => {
    const rows = buildRunLiveTimeline({
      activities: [{
        id: 'activity-1', kind: 'EXECUTE', status: 'RUNNING',
        created_at: '2026-09-13T01:00:00Z', updated_at: '2026-09-13T01:04:00Z',
      }],
      invocations: [{
        id: 'invocation-1', status: 'SUCCEEDED', invocation_seq: 2,
        model_id: 'deepseek-flash', provider_ref: 'deepseek:api',
        assistant_text: '已定位重复创建订单的入口，下一步检查幂等键。',
        tool_calls: [{name: 'read_file', arguments: {path: 'orders.py'}}],
        created_at: '2026-09-13T01:01:00Z', updated_at: '2026-09-13T01:02:00Z',
      }],
      effects: [{
        id: 'effect-1', status: 'SUCCEEDED', tool_ref: 'run_tests',
        evidence_ids: ['artifact-1', 'artifact-2'],
        created_at: '2026-09-13T01:02:30Z', updated_at: '2026-09-13T01:03:00Z',
      }],
      commands: [{
        id: 'command-1', kind: 'START', status: 'SUCCEEDED',
        created_at: '2026-09-13T00:59:00Z', updated_at: '2026-09-13T00:59:30Z',
      }],
    });

    expect(rows.map((row) => row.title)).toEqual([
      '正在执行任务',
      '测试命令执行成功',
      '模型已完成第 2 次工作',
      '开始运行目标',
    ]);
    expect(rows[1]).toMatchObject({
      summary: '命令结果已保存为 2 份证据，可在验收页核对原始输出。',
      tone: 'success',
    });
    expect(rows[2]?.summary).toContain('已定位重复创建订单的入口');
    expect(rows[2]?.details).toContain('读取文件');
    expect(JSON.stringify(rows)).not.toContain('chain-of-thought');
  });

  it('未知状态使用明确的等待和人工核对文案', () => {
    const rows = buildRunLiveTimeline({
      activities: [], invocations: [], commands: [],
      effects: [{
        id: 'effect-unknown', status: 'UNKNOWN', tool_ref: 'write_file',
        evidence_ids: [], created_at: null, updated_at: null,
      }],
    });
    expect(rows[0]).toMatchObject({
      title: '文件写入结果还不能确认',
      summary: '系统会先核对已经发生的结果，不会直接再执行一次。',
      tone: 'warning',
    });
  });

  it('连接正常与业务推进分别判断', () => {
    expect(assessRunProgress('RUNNING', [
      {id: 'execute', kind: 'EXECUTE', status: 'RUNNING', updated_at: '2026-09-13T01:05:00Z'},
    ])).toMatchObject({state: 'active', label: '系统记录有步骤正在执行'});
    expect(assessRunProgress('RUNNING', [
      {id: 'execute', kind: 'EXECUTE', status: 'READY', updated_at: '2026-09-13T01:05:00Z'},
    ])).toMatchObject({state: 'queued', label: '任务正在等待执行器'});
  });
});
