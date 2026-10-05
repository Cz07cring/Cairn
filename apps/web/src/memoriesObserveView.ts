/**
 * 记忆列表投影。
 * Memory VERIFIED ≠ Goal DONE。
 */
export type MemoryRow = {
  id: string;
  kind: string;
  status: string;
  statement: string;
  confidenceBp: number;
  sourceEvidenceCount: number;
  updatedAt: string | null;
};

export type MemoryListItem = {
  id: string;
  kind: string;
  status: string;
  statement?: string;
  confidence_bp?: number;
  source_evidence_ids?: string[];
  updated_at?: string | null;
};

export function memoryRowsFromList(items: MemoryListItem[]): MemoryRow[] {
  return items.map((item) => ({
    id: item.id,
    kind: String(item.kind ?? ''),
    status: String(item.status ?? ''),
    statement: String(item.statement ?? '').trim() || '(无 statement)',
    confidenceBp: Number(item.confidence_bp ?? 0),
    sourceEvidenceCount: Array.isArray(item.source_evidence_ids)
      ? item.source_evidence_ids.length
      : 0,
    updatedAt: item.updated_at ?? null,
  }));
}

export function countVerifiedMemories(rows: MemoryRow[]): number {
  return rows.filter((r) => r.status === 'VERIFIED').length;
}

export function memoriesCaption(input: {
  rowCount: number;
  verifiedCount: number;
  kindFilter: string | null;
}): string {
  const kind = input.kindFilter ? `kind=${input.kindFilter}；` : '全部 kind；';
  return `${kind}共 ${input.rowCount} 条，其中 VERIFIED ${input.verifiedCount}。Memory VERIFIED ≠ Goal DONE；PROPOSED 亦非终局。`;
}

export function memoryStatusCaption(status: string): string {
  if (status === 'VERIFIED') {
    return 'VERIFIED：记忆已晋升；≠ Goal DONE。';
  }
  if (status === 'PROPOSED') {
    return 'PROPOSED：提议记忆，未晋升；≠ DONE。';
  }
  if (status === 'SUPERSEDED') {
    return 'SUPERSEDED：已被替代；≠ 终局裁决。';
  }
  return `${status}：只读观察。`;
}
