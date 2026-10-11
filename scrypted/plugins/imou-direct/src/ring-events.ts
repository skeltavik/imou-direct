import { CapabilityGate } from './capabilities';

export interface RingEventSource {
  subscribe(listener: () => void): () => void;
}

export class RingEventController {
  private unsubscribe?: () => void;
  private pulseTimer?: ReturnType<typeof setTimeout>;
  private active = false;

  constructor(
    private readonly gate: CapabilityGate,
    private readonly source: RingEventSource,
    private readonly publish: (active: boolean) => void,
    private readonly pulseMs: number,
  ) {
    if (!Number.isSafeInteger(pulseMs) || pulseMs <= 0) {
      throw new Error('ring pulse duration must be a positive integer');
    }
  }

  start(): void {
    if (this.unsubscribe) return;
    this.gate.require('doorbellEvents');
    this.unsubscribe = this.source.subscribe(() => this.onPress());
  }

  stop(): void {
    if (!this.unsubscribe) return;
    const unsubscribe = this.unsubscribe;
    try {
      unsubscribe();
      this.unsubscribe = undefined;
    } finally {
      if (this.pulseTimer) clearTimeout(this.pulseTimer);
      this.pulseTimer = undefined;
      if (this.active) {
        this.active = false;
        this.publish(false);
      }
    }
  }

  private onPress(): void {
    if (this.active) return;
    this.active = true;
    this.publish(true);
    this.pulseTimer = setTimeout(() => {
      this.pulseTimer = undefined;
      this.active = false;
      this.publish(false);
    }, this.pulseMs);
  }
}
