import { describe, expect, it, vi } from 'vitest';
import { closeDeviceResources } from '../src/device-lifecycle';

describe('closeDeviceResources', () => {
  it('stops intercom even when ring cleanup throws', async () => {
    const stopRing = vi.fn(() => {
      throw new Error('ring cleanup failed');
    });
    const stopIntercom = vi.fn(async () => undefined);

    await expect(closeDeviceResources(stopRing, stopIntercom)).rejects.toThrowError(
      'ring cleanup failed',
    );

    expect(stopRing).toHaveBeenCalledOnce();
    expect(stopIntercom).toHaveBeenCalledOnce();
  });
});
