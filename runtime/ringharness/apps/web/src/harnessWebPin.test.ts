import {describe, expect, it} from 'vitest';
import {RING_HARNESS_WEB_PIN_SHA, harnessWebPinCaption, resolveHarnessWebPin} from './harnessWebPin.js';

describe('harnessWebPin', () => {
  it('defaults to local dsh web origin and frozen pin', () => {
    const pin = resolveHarnessWebPin({});
    expect(pin.openUrl).toBe('http://127.0.0.1:3080');
    expect(pin.pinSha).toBe(RING_HARNESS_WEB_PIN_SHA);
    expect(pin.configured).toBe(false);
    expect(pin.pinShaShort).toBe('c291e796');
  });

  it('uses VITE_RING_HARNESS_WEB_URL when set (tokenized URL allowed)', () => {
    const pin = resolveHarnessWebPin({
      VITE_RING_HARNESS_WEB_URL: 'http://127.0.0.1:3080/?token=abc',
      VITE_RING_HARNESS_PIN: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    });
    expect(pin.openUrl).toBe('http://127.0.0.1:3080/?token=abc');
    expect(pin.configured).toBe(true);
    expect(pin.pinShaShort).toBe('aaaaaaaa');
  });

  it('caption keeps DONE boundary and token hint', () => {
    const text = harnessWebPinCaption(resolveHarnessWebPin({}));
    expect(text).toContain('DeepSeek Harness Web');
    expect(text).toContain('≠ Goal DONE');
    expect(text).toContain('401');
  });
});
