import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CapabilityGate } from '../src/capabilities';
import { RingEventController, type RingEventSource } from '../src/ring-events';

class FakeRingSource implements RingEventSource {
  listener?: () => void;
  unsubscribeCount = 0;

  subscribe(listener: () => void): () => void {
    this.listener = listener;
    return () => {
      this.unsubscribeCount += 1;
      this.listener = undefined;
    };
  }

  press(): void {
    this.listener?.();
  }
}

describe('RingEventController', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('emits one bounded pulse and suppresses duplicate presses during it', () => {
    const source = new FakeRingSource();
    const states: boolean[] = [];
    const gate = new CapabilityGate({
      video: false,
      inboundAudio: false,
      doorbellEvents: true,
      intercom: false,
    });
    const controller = new RingEventController(gate, source, state => states.push(state), 1_000);

    controller.start();
    source.press();
    source.press();
    vi.advanceTimersByTime(999);
    expect(states).toEqual([true]);
    vi.advanceTimersByTime(1);
    expect(states).toEqual([true, false]);

    controller.stop();
    controller.stop();
    expect(source.unsubscribeCount).toBe(1);
  });

  it('refuses to subscribe when local doorbell events are unverified', () => {
    const source = new FakeRingSource();
    const controller = new RingEventController(
      new CapabilityGate(),
      source,
      () => undefined,
      1_000,
    );

    expect(() => controller.start()).toThrowError(
      'doorbellEvents is not locally verified',
    );
    expect(source.listener).toBeUndefined();
  });

  it('clears a live pulse even when unsubscribe fails', () => {
    let subscriptions = 0;
    let unsubscribeAttempts = 0;
    const source: RingEventSource & { press(): void } = {
      press: () => undefined,
      subscribe(listener): () => void {
        subscriptions += 1;
        this.press = listener;
        return () => {
          unsubscribeAttempts += 1;
          if (unsubscribeAttempts === 1) throw new Error('unsubscribe failed');
        };
      },
    };
    const states: boolean[] = [];
    const gate = new CapabilityGate({
      video: false,
      inboundAudio: false,
      doorbellEvents: true,
      intercom: false,
    });
    const controller = new RingEventController(gate, source, state => states.push(state), 1_000);
    controller.start();
    source.press();

    expect(() => controller.stop()).toThrowError('unsubscribe failed');
    expect(states).toEqual([true, false]);
    vi.advanceTimersByTime(1_000);
    expect(states).toEqual([true, false]);

    controller.start();
    expect(subscriptions).toBe(1);
    controller.stop();
    expect(unsubscribeAttempts).toBe(2);
  });
});
