/**
 * Task Evidence 列表投影；信封行 ≠ PASS ≠ Goal DONE。
 */

export type EvidenceRow = {
  id: string;
  effectId: string;
  exitCode: number | null;
  timedOut: boolean;
  candidateManifestId: string;
  contentDigest: string;
  producerIdentity: string;
  artifactCount: number;
  commandPreview: string;
};

export type EvidenceListItem = {
  id: string;
  effect_id: string;
  exit_code?: number | null;
  timed_out?: boolean;
  candidate_manifest_id?: string | null;
  content_digest: string;
  producer_identity: string;
  artifact_ids?: string[];
  command_argv?: string[];
};

export function evidenceRowsFromList(items: EvidenceListItem[]): EvidenceRow[] {
  return items.map((item) => ({
    id: item.id,
    effectId: String(item.effect_id ?? ''),
    exitCode: item.exit_code == null ? null : Number(item.exit_code),
    timedOut: Boolean(item.timed_out),
    candidateManifestId:
      item.candidate_manifest_id == null
        ? ''
        : String(item.candidate_manifest_id),
    contentDigest: String(item.content_digest ?? ''),
    producerIdentity: String(item.producer_identity ?? ''),
    artifactCount: Array.isArray(item.artifact_ids)
      ? item.artifact_ids.length
      : 0,
    commandPreview: Array.isArray(item.command_argv)
      ? item.command_argv.slice(0, 4).join(' ')
      : '',
  }));
}

export function evidenceCaption(input: {
  rowCount: number;
  timedOutCount: number;
}): string {
  return (
    `共 ${input.rowCount} 条证据信封；timed_out ${input.timedOutCount}。` +
    `EvidenceEnvelope 行 ≠ Audit PASS ≠ Goal DONE。`
  );
}

export function evidenceExitCaption(
  exitCode: number | null,
  timedOut: boolean,
): string {
  if (timedOut) {
    return 'timed_out：超时观察；≠ 验收裁决。';
  }
  if (exitCode === 0) {
    return 'exit_code=0：命令成功退出；≠ Audit PASS ≠ Goal DONE。';
  }
  if (exitCode == null) {
    return 'exit_code 空：只读观察；≠ DONE。';
  }
  return `exit_code=${exitCode}：命令非零退出；≠ Goal FAILED 裁决。`;
}
