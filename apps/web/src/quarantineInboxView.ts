/**
 * Quarantine inbox / SystemStatus 投影；只读信号 ≠ 义务结算 ≠ DONE。
 */
export type QuarantineComponentView = {
  name: string;
  state: string;
  reasonCode: string | null;
};

export function quarantineComponentFromStatus(status: {
  components: Array<{
    name: string;
    state: string;
    reason_code?: string | null;
  }>;
}): QuarantineComponentView | null {
  const hit = status.components.find(
    (c) => c.name === 'verification_obligation_quarantine',
  );
  if (!hit) {
    return null;
  }
  return {
    name: hit.name,
    state: hit.state,
    reasonCode: hit.reason_code ?? null,
  };
}

export function quarantineInboxCaption(input: {
  component: QuarantineComponentView | null;
  rowCount: number;
}): string {
  if (input.component == null) {
    return `已列 ${input.rowCount} 条 QUARANTINED 义务；缺 SystemStatus 组件。列表 ≠ 结算 ≠ DONE。`;
  }
  if (input.component.state === 'HEALTHY' && input.rowCount === 0) {
    return 'verification_obligation_quarantine=HEALTHY；无隔离义务。健康 ≠ Goal DONE。';
  }
  return (
    `verification_obligation_quarantine=${input.component.state}` +
    (input.component.reasonCode ? `（${input.component.reasonCode}）` : '') +
    `；inbox ${input.rowCount} 条。DEGRADED/隔离只读信号 ≠ 已验收 ≠ DONE。`
  );
}

export function obligationRowLabel(row: {
  id: string;
  status: string;
  layer: string;
  audit_round: number;
}): string {
  return `${row.status} · ${row.layer} · round=${row.audit_round} (${row.id.slice(0, 8)}…)`;
}
