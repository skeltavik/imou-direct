import { describe, expect, it } from 'vitest';
import { CapabilityGate } from '../src/capabilities';

describe('CapabilityGate', () => {
  it('keeps every unproven runtime capability disabled', () => {
    const gate = new CapabilityGate();

    expect(gate.snapshot()).toEqual({
      video: false,
      inboundAudio: false,
      doorbellEvents: false,
      intercom: false,
    });
    expect(() => gate.require('video')).toThrowError('video is not locally verified');
    expect(() => gate.require('doorbellEvents')).toThrowError(
      'doorbellEvents is not locally verified',
    );
    expect(() => gate.require('intercom')).toThrowError(
      'intercom is not locally verified',
    );
  });

  it('does not change when the caller mutates its original evidence object', () => {
    const evidence = {
      video: false,
      inboundAudio: false,
      doorbellEvents: false,
      intercom: false,
    };
    const gate = new CapabilityGate(evidence);

    evidence.video = true;

    expect(() => gate.require('video')).toThrowError(
      'video is not locally verified',
    );
  });
});
