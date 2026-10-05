/**
 * 固定 pin 的 DeepSeek Harness Web 执行面入口（三面解耦）。
 * - 管理：Hatchet 驾驶舱
 * - 执行：官方 dsh web（对话流 / reasoning；本仓不维护其 UI）
 * - 治理：Kernel / Control（DONE 只经 VerificationProfile + 屏障）
 *
 * 深链打开官方 Web；不 iframe（进程 token 须用 dsh 打印的 URL）。
 * session 成功 ≠ Goal DONE。
 */

/** 与 AGENTS.md / doc/09 钉死的上游 SHA 一致；仅作展示与核对，不替代 RING_HARNESS_CHECKOUT。 */
export const RING_HARNESS_WEB_PIN_SHA =
  'c291e7961a515f6d7af9304e7fd1d257929aef26';

const DEFAULT_HARNESS_WEB_ORIGIN = 'http://127.0.0.1:3080';

export type HarnessWebPinConfig = {
  /** 浏览器可打开的 origin 或完整 URL（可含 dsh 进程 token query）。 */
  openUrl: string;
  pinShaShort: string;
  pinSha: string;
  configured: boolean;
};

function trimUrl(raw: string | undefined): string {
  return (raw ?? '').trim().replace(/\/$/, '');
}

/** 从 Vite 环境解析执行面 URL；缺省本机 dsh web 默认端口。 */
export function resolveHarnessWebPin(env: {
  VITE_RING_HARNESS_WEB_URL?: string;
  VITE_RING_HARNESS_PIN?: string;
} = {}): HarnessWebPinConfig {
  const configured = Boolean(trimUrl(env.VITE_RING_HARNESS_WEB_URL));
  const openUrl = trimUrl(env.VITE_RING_HARNESS_WEB_URL) || DEFAULT_HARNESS_WEB_ORIGIN;
  const pinSha =
    (env.VITE_RING_HARNESS_PIN ?? '').trim() || RING_HARNESS_WEB_PIN_SHA;
  return {
    openUrl,
    pinSha,
    pinShaShort: pinSha.slice(0, 8),
    configured,
  };
}

export function harnessWebPinCaption(pin: HarnessWebPinConfig): string {
  return (
    `执行面使用固定 pin ${pin.pinShaShort}… 的官方 DeepSeek Harness Web（对话流由其托管）。` +
    `本页只读 Kernel 摘要，不展示私有推理；打开官方 Web 后请用 dsh 打印的带 token 地址（裸开 :3080 会 401）。` +
    `对话完成 ≠ Goal DONE。`
  );
}
