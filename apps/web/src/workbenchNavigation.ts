export type WorkbenchPage =
  | 'overview'
  | 'goals'
  | 'attention'
  | 'runs'
  | 'run-overview'
  | 'run-live'
  | 'run-trace'
  | 'run-logs'
  | 'run-evidence'
  | 'health';

const LEGACY_HASHES: Record<string, WorkbenchPage> = {
  tasks: 'run-overview',
  plans: 'run-overview',
  activities: 'run-trace',
  effects: 'run-trace',
  commands: 'run-logs',
  'goal-events': 'run-logs',
  'task-evidence': 'run-evidence',
  'goal-reviews': 'run-evidence',
  finalization: 'run-evidence',
  'quarantine-inbox': 'attention',
  approvals: 'attention',
  workspace: 'overview',
};

const PAGES = new Set<WorkbenchPage>([
  'overview',
  'goals',
  'attention',
  'run-overview',
  'run-live',
  'run-trace',
  'run-logs',
  'run-evidence',
  'runs',
  'health',
]);

/** 将 URL hash 收敛为产品工作台页面，并兼容旧面板锚点。 */
export function parseWorkbenchHash(hash: string): WorkbenchPage {
  const raw = hash.replace(/^#\/?/, '').split('?')[0]?.trim() ?? '';
  if (raw in LEGACY_HASHES) {
    return LEGACY_HASHES[raw];
  }
  return PAGES.has(raw as WorkbenchPage) ? (raw as WorkbenchPage) : 'overview';
}

export function workbenchHash(page: WorkbenchPage): string {
  return `#${page}`;
}

export function isRunPage(page: WorkbenchPage): boolean {
  return page.startsWith('run-');
}

export function readWorkbenchParam(hash: string, name: string): string | null {
  const query = hash.split('?')[1] ?? '';
  return new URLSearchParams(query).get(name);
}

export function withWorkbenchParam(page: WorkbenchPage, name: string, value?: string): string {
  const params = new URLSearchParams();
  if (value) params.set(name, value);
  const query = params.toString();
  return `${workbenchHash(page)}${query ? `?${query}` : ''}`;
}
