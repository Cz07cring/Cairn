/**
 * heartbeat STOP → 关闸 + harness RUNNING 回执；≠ EXITED / ≠ DONE。
 */
import {describe, expect, test, vi} from 'vitest';
import {applyHeartbeatStopControl} from './heartbeatStopHandler.js';
import type {StopSeamPorts} from './stopSeam.js';

describe('applyHeartbeatStopControl', () => {
  test('非 STOP 零副作用', async () => {
    const gate = {onHeartbeatFailure: vi.fn()};
    const stopSeam = {
      loadStop: vi.fn(),
      postReceipt: vi.fn(),
    } satisfies StopSeamPorts;
    const out = await applyHeartbeatStopControl({
      control: 'CONTINUE',
      pendingStopIds: ['s1'],
      gate,
      stopSeam,
      activationId: 'a',
      attemptId: 't',
    });
    expect(out.closedAdmission).toBe(false);
    expect(out.marksGoalDone).toBe(false);
    expect(gate.onHeartbeatFailure).not.toHaveBeenCalled();
    expect(stopSeam.loadStop).not.toHaveBeenCalled();
  });

  test('STOP + pending → 关闸并交 RUNNING 回执', async () => {
    const gate = {onHeartbeatFailure: vi.fn()};
    const postReceipt = vi.fn(async () => ({disposition: 'APPLIED'}));
    const stopSeam: StopSeamPorts = {
      loadStop: vi.fn(async () => ({
        id: 'stop-1',
        status: 'REQUESTED' as const,
        state_revision: 1,
        receipt_ids: [],
      })),
      postReceipt,
    };

    const out = await applyHeartbeatStopControl({
      control: 'STOP',
      pendingStopIds: ['stop-1'],
      gate,
      stopSeam,
      activationId: 'act-1',
      attemptId: 'att-1',
      newReceiptId: () => 'receipt-1',
    });

    expect(out.closedAdmission).toBe(true);
    expect(out.marksGoalDone).toBe(false);
    expect(out.observations).toEqual([
      {stopId: 'stop-1', observation: 'RUNNING', disposition: 'APPLIED'},
    ]);
    expect(gate.onHeartbeatFailure).toHaveBeenCalledWith(
      expect.objectContaining({
        message: expect.stringMatching(/HEARTBEAT_CONTROL_STOP:stop-1/),
      }),
    );
    expect(postReceipt).toHaveBeenCalledOnce();
    expect(postReceipt).toHaveBeenCalledWith(
      expect.objectContaining({
        observation: 'RUNNING',
        compute_released: false,
        stop_id: 'stop-1',
        receipt_id: 'receipt-1',
      }),
    );
  });

  test('STOP 无 stopSeam 仍关闸', async () => {
    const gate = {onHeartbeatFailure: vi.fn()};
    const out = await applyHeartbeatStopControl({
      control: 'STOP',
      pendingStopIds: ['s'],
      gate,
      activationId: 'a',
      attemptId: 't',
    });
    expect(out.closedAdmission).toBe(true);
    expect(out.observations).toEqual([]);
    expect(gate.onHeartbeatFailure).toHaveBeenCalledOnce();
  });
});
