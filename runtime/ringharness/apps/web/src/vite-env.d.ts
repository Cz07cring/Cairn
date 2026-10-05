/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_RING_GOAL_ID?: string;
  readonly VITE_RING_DEV_BEARER?: string;
  /** 官方 dsh web 打开地址（可含进程 token query）；缺省 http://127.0.0.1:3080 */
  readonly VITE_RING_HARNESS_WEB_URL?: string;
  /** 展示用 pin SHA；缺省与 AGENTS/doc/09 钉死 SHA 一致 */
  readonly VITE_RING_HARNESS_PIN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
