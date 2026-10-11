import { describe, expect, it } from 'vitest';
import { CapabilityGate } from '../src/capabilities';
import { createDoorbellDescriptor } from '../src/device-descriptor';

describe('createDoorbellDescriptor', () => {
  it('advertises no unverified camera, event, or intercom interfaces', () => {
    const descriptor = createDoorbellDescriptor('imou-doorbell', new CapabilityGate());

    expect(descriptor.type).toBe('Doorbell');
    expect(descriptor.interfaces).toEqual(['Settings', 'Online']);
  });

  it('advertises each interface only after its independent evidence gate opens', () => {
    const descriptor = createDoorbellDescriptor(
      'imou-doorbell',
      new CapabilityGate({
        video: true,
        inboundAudio: true,
        doorbellEvents: true,
        intercom: true,
      }),
    );

    expect(descriptor.interfaces).toEqual([
      'Settings',
      'Online',
      'VideoCamera',
      'BinarySensor',
      'Intercom',
    ]);
  });
});
