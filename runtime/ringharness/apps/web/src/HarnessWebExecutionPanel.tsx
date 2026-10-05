/**
 * 执行面入口：深链打开固定 pin 的官方 dsh web（对话流 / thinking）。
 * 不嵌入 iframe（须进程 token）；不维护上游 UI；≠ Goal DONE。
 */
import {harnessWebPinCaption, resolveHarnessWebPin} from './harnessWebPin.js';

export function HarnessWebExecutionPanel() {
  const pin = resolveHarnessWebPin({
    VITE_RING_HARNESS_WEB_URL: import.meta.env.VITE_RING_HARNESS_WEB_URL,
    VITE_RING_HARNESS_PIN: import.meta.env.VITE_RING_HARNESS_PIN,
  });
  return (
    <article className="wb-surface wb-harness-exec" aria-labelledby="harness-exec-title">
      <header>
        <div>
          <small>执行面 · DeepSeek Harness Web（固定 pin）</small>
          <h2 id="harness-exec-title">Agent 对话流与思考过程</h2>
          <p>
            对话、reasoning、工具轨迹由官方 UI 托管；Ringharness 只做管理与治理。
            pin <code>{pin.pinShaShort}…</code>
            {pin.configured ? ' · 已配置打开地址' : ' · 默认本机 :3080'}
          </p>
        </div>
        <a
          className="wb-popout"
          href={pin.openUrl}
          target="_blank"
          rel="noreferrer"
        >
          打开 Harness Web ↗
        </a>
      </header>
      <p className="wb-harness-exec-caption">{harnessWebPinCaption(pin)}</p>
      <ol className="wb-harness-exec-steps">
        <li>
          在固定 checkout 启动：
          <code>bash scripts/serve_harness_web.sh</code>
          （或 <code>dsh --profile web --no-open</code>）
        </li>
        <li>使用终端打印的<strong>带 token</strong> URL（可写入 <code>VITE_RING_HARNESS_WEB_URL</code> 后重启 Vite）</li>
        <li>本页下方时间线仍只读 Kernel 摘要，与官方对话流解耦</li>
      </ol>
    </article>
  );
}
