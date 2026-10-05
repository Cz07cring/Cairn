import {expect, test, vi} from 'vitest';
import type {RuntimeEvent} from './activation.js';
import type {StopReceiptView} from './stopSeam.js';
import {observeStop} from './stopSeam.js';

test('fake adapter 可诚实报告 EXITED+compute_released', async () => {
  // mock 形参必须带类型，否则 tsc 把 mock.calls 推成空元组，CI build 失败
  const postReceipt = vi.fn(async (_body: StopReceiptView) => ({disposition: 'APPLIED'}));
  const emitRuntimeEvent = vi.fn(async (_event: RuntimeEvent) => undefined);
  const result = await observeStop(
    {
      loadStop: async () => ({
        id: 's1',
        status: 'REQUESTED',
        state_revision: 1,
        receipt_ids: [],
      }),
      postReceipt,
      emitRuntimeEvent,
    },
    {
      stopId: 's1',
      adapter: 'fake',
      activationId: 'a1',
      attemptId: 'a1',
      receiptId: 'r1',
      nowIso: '2026-09-11T12:00:00.000Z',
    },
  );
  expect(result.observation).toBe('EXITED');
  expect(result.disposition).toBe('APPLIED');
  expect(postReceipt).toHaveBeenCalledOnce();
  const body = postReceipt.mock.calls[0][0];
  expect(body.compute_released).toBe(true);
  expect(body.write_capability_revoked).toBe(true);
  expect(emitRuntimeEvent).toHaveBeenCalledOnce();
  expect(emitRuntimeEvent.mock.calls[0][0].type).toBe('STOP_OBSERVED');
});

test('harness adapter 未杀进程时不得假 EXITED', async () => {
  const postReceipt = vi.fn(async (_body: StopReceiptView) => ({disposition: 'APPLIED'}));
  const result = await observeStop(
    {
      loadStop: async () => ({
        id: 's2',
        status: 'REQUESTED',
        state_revision: 1,
        receipt_ids: [],
      }),
      postReceipt,
    },
    {
      stopId: 's2',
      adapter: 'harness',
      activationId: 'a2',
      attemptId: 'a2',
      receiptId: 'r2',
    },
  );
  expect(result.observation).toBe('RUNNING');
  const body = postReceipt.mock.calls[0][0];
  expect(body.compute_released).toBe(false);
  expect(body.observation).toBe('RUNNING');
});

test('已 CONFIRMED 时不重复推观察', async () => {
  const postReceipt = vi.fn(async (_body: StopReceiptView) => ({disposition: 'APPLIED'}));
  const result = await observeStop(
    {
      loadStop: async () => ({
        id: 's3',
        status: 'CONFIRMED',
        state_revision: 2,
        receipt_ids: ['r0'],
      }),
      postReceipt,
    },
    {
      stopId: 's3',
      adapter: 'fake',
      activationId: 'a3',
      attemptId: 'a3',
      receiptId: 'r3',
    },
  );
  expect(result.disposition).toBe('DUPLICATE');
  expect(postReceipt).not.toHaveBeenCalled();
});
