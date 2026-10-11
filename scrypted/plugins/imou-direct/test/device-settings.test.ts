import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@scrypted/sdk', () => {
  class ScryptedDeviceBaseShim {
    binaryState = false;
    online = false;
    constructor(..._args: unknown[]) {}
  }
  const sdkShim = {
    deviceManager: { onDevicesChanged: vi.fn(async () => undefined) },
    mediaManager: {
      createFFmpegMediaObject: vi.fn(async (input: unknown) => ({ input })),
    },
  };
  return { __esModule: true, default: sdkShim, ScryptedDeviceBase: ScryptedDeviceBaseShim };
});

import type { Setting } from '@scrypted/sdk';
import { ImouDoorbellDevice } from '../src/main';

function settingsByKey(settings: Setting[]): Record<string, Setting | undefined> {
  const table: Record<string, Setting | undefined> = {};
  for (const setting of settings) {
    if (setting.key) table[setting.key] = setting;
  }
  return table;
}

describe('ImouDoorbellDevice intercept settings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('exposes the daemon endpoint alongside read-only evidence gates', async () => {
    const device = new ImouDoorbellDevice('test-device');
    const byKey = settingsByKey(await device.getSettings());

    expect(byKey['interceptHost']?.value).toBe('127.0.0.1');
    expect(byKey['interceptPort']?.value).toBe(49511);
    expect(byKey['evidence-video']?.value).toBe(true);
    expect(byKey['evidence-intercom']?.value).toBe(false);
  });

  it('rejects evidence gate overrides', async () => {
    const device = new ImouDoorbellDevice('test-device');
    await expect(device.putSetting('evidence-video', false)).rejects.toThrowError(
      'evidence gates are read-only',
    );
  });

  it('rejects an invalid intercept port', async () => {
    const device = new ImouDoorbellDevice('test-device');
    await expect(device.putSetting('interceptPort', 0)).rejects.toThrowError(
      'intercept port is invalid',
    );
  });

  it('keeps video verified after an endpoint change', async () => {
    const device = new ImouDoorbellDevice('test-device');
    await device.putSetting('interceptHost', 'daemon.lan');
    await device.putSetting('interceptPort', 49512);
    const byKey = settingsByKey(await device.getSettings());

    expect(byKey['interceptHost']?.value).toBe('daemon.lan');
    expect(byKey['interceptPort']?.value).toBe(49512);
    expect(byKey['evidence-video']?.value).toBe(true);
  });

  it('injects a custom transport for offline construction', async () => {
    const open = vi.fn(async () => {
      throw new Error('custom transport unused');
    });
    const device = new ImouDoorbellDevice('test-device', undefined, undefined, { open });
    await expect(device.getVideoStream()).rejects.toThrowError('custom transport unused');
    expect(open).toHaveBeenCalledOnce();
  });
});
