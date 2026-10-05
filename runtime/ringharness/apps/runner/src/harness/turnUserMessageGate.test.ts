import {expect, test} from 'vitest';
import {createTurnUserMessageGate} from './turnUserMessageGate.js';

test('AB08：accepting 时消息原子挂到当前 Turn，不丢失', async () => {
  const gate = createTurnUserMessageGate({turnId: 'turn-a'});
  const d = await gate.offer({messageId: 'm1', text: 'hello'});
  expect(d).toEqual({
    kind: 'attached',
    turnId: 'turn-a',
    messageId: 'm1',
    marksGoalDone: false,
  });
  expect(gate.peekAttached()).toEqual([{messageId: 'm1', text: 'hello'}]);
  const sealed = await gate.seal();
  expect(sealed.attached).toHaveLength(1);
  expect(sealed.marksGoalDone).toBe(false);
  expect(gate.isAccepting()).toBe(false);
});

test('AB08：seal 后再到的消息开新 Turn，不在旧 Turn 双跑', async () => {
  let n = 0;
  const gate = createTurnUserMessageGate({
    turnId: 'turn-old',
    newTurnId: () => `turn-new-${++n}`,
  });
  await gate.seal();
  const d = await gate.offer({messageId: 'late-1', text: 'after seal'});
  expect(d).toEqual({
    kind: 'deferred_new_turn',
    turnId: 'turn-new-1',
    priorTurnId: 'turn-old',
    messageId: 'late-1',
    marksGoalDone: false,
  });
  expect(gate.peekAttached()).toEqual([]);
  const next = gate.takeDeferredTurn();
  expect(next).toEqual({
    turnId: 'turn-new-1',
    messages: [{messageId: 'late-1', text: 'after seal'}],
  });
  expect(gate.takeDeferredTurn()).toBeNull();
});

test('AB08：同 messageId 幂等 duplicate，不双挂', async () => {
  const gate = createTurnUserMessageGate({turnId: 'turn-dup'});
  await gate.offer({messageId: 'same', text: 'once'});
  const again = await gate.offer({messageId: 'same', text: 'twice'});
  expect(again.kind).toBe('duplicate');
  expect(again.marksGoalDone).toBe(false);
  expect(gate.peekAttached()).toHaveLength(1);
});

test('AB08：seal 与 offer 尾部竞态——每条消息恰好一种处置，不丢不双跑', async () => {
  let nextId = 0;
  const gate = createTurnUserMessageGate({
    turnId: 'turn-race',
    newTurnId: () => `spawned-${++nextId}`,
  });

  const [d1, sealed, d2, d3] = await Promise.all([
    gate.offer({messageId: 'race-a', text: 'a'}),
    gate.seal(),
    gate.offer({messageId: 'race-b', text: 'b'}),
    gate.offer({messageId: 'race-c', text: 'c'}),
  ]);

  expect(sealed.marksGoalDone).toBe(false);
  const dispositions = [d1, d2, d3];
  const byId = new Map(dispositions.map((d) => [d.messageId, d.kind]));
  expect(byId.size).toBe(3);
  for (const id of ['race-a', 'race-b', 'race-c']) {
    const kind = byId.get(id);
    expect(kind === 'attached' || kind === 'deferred_new_turn').toBe(true);
  }

  const attachedIds = new Set(sealed.attached.map((m) => m.messageId));
  const deferred = gate.takeDeferredTurn();
  const deferredIds = new Set((deferred?.messages ?? []).map((m) => m.messageId));

  // 不双跑：attached ∩ deferred = ∅
  for (const id of attachedIds) {
    expect(deferredIds.has(id)).toBe(false);
  }
  // 不丢失：三者并集覆盖全部 messageId
  expect(new Set([...attachedIds, ...deferredIds])).toEqual(
    new Set(['race-a', 'race-b', 'race-c']),
  );

  for (const id of attachedIds) {
    expect(byId.get(id)).toBe('attached');
  }
  for (const id of deferredIds) {
    expect(byId.get(id)).toBe('deferred_new_turn');
  }
});

test('空 turnId / messageId 失败关闭', async () => {
  expect(() => createTurnUserMessageGate({turnId: ''})).toThrow(/TURN_ID_REQUIRED/);
  const gate = createTurnUserMessageGate({turnId: 't'});
  await expect(gate.offer({messageId: '', text: 'x'})).rejects.toThrow(
    /MESSAGE_ID_REQUIRED/,
  );
});
