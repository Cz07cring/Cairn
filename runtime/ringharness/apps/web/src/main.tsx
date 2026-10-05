import {createRoot} from 'react-dom/client';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {WorkbenchApp} from './WorkbenchApp.js';
import './style.css';

const queryClient = new QueryClient({
  defaultOptions: {queries: {retry: 1, refetchOnWindowFocus: true}},
});

/** 入口：Codex Hatchet 驾驶舱（PR #64）；临时 WorkbenchShell 已弃用。 */
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <WorkbenchApp />
  </QueryClientProvider>,
);
