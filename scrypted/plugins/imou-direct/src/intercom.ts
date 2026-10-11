import type { MediaObject } from '@scrypted/sdk';
import { CapabilityGate } from './capabilities';

export interface TalkbackFormat {
  readonly codec: 'pcma';
  readonly sampleRate: 8000;
  readonly channels: 1;
}

export interface EncodedTalkback {
  readonly format: TalkbackFormat;
  readonly stream: AsyncIterable<Uint8Array>;
  close(): Promise<void>;
}

export interface TalkbackEncoder {
  encode(media: MediaObject): Promise<EncodedTalkback>;
}

export interface TalkbackSession {
  stop(): Promise<void>;
}

export interface TalkbackTransport {
  start(source: EncodedTalkback): Promise<TalkbackSession>;
}

interface ActiveIntercom {
  source: EncodedTalkback;
  session: TalkbackSession;
}

export class IntercomController {
  private active?: ActiveIntercom;
  private startPromise?: Promise<void>;
  private stopPromise?: Promise<void>;
  private stopRequested = false;
  private deadlineTimer?: ReturnType<typeof setTimeout>;

  constructor(
    private readonly gate: CapabilityGate,
    private readonly encoder: TalkbackEncoder,
    private readonly transport: TalkbackTransport,
    private readonly maximumDurationMs = 5 * 60_000,
  ) {
    if (!Number.isSafeInteger(maximumDurationMs) || maximumDurationMs <= 0) {
      throw new Error('intercom maximum duration must be a positive integer');
    }
  }

  async start(media: MediaObject): Promise<void> {
    this.gate.require('intercom');
    if (this.startPromise || this.stopPromise || this.active) {
      throw new Error('intercom session already active');
    }

    this.stopRequested = false;
    const operation = this.startInternal(media);
    this.startPromise = operation;
    void operation.then(
      () => this.clearStartPromise(operation),
      () => this.clearStartPromise(operation),
    );
    await operation;
  }

  private clearStartPromise(operation: Promise<void>): void {
    if (this.startPromise === operation) this.startPromise = undefined;
  }

  private async startInternal(media: MediaObject): Promise<void> {
    let source: EncodedTalkback | undefined;
    try {
      source = await this.encoder.encode(media);
      this.validateFormat(source.format);
      const session = await this.transport.start(source);
      const pending = { source, session };
      source = undefined;
      if (this.stopRequested) {
        try {
          await this.closeActive(pending);
        } catch (error) {
          this.active = pending;
          throw error;
        }
        return;
      }
      this.active = pending;
      this.deadlineTimer = setTimeout(() => {
        void this.stop().catch(() => undefined);
      }, this.maximumDurationMs);
    } catch (error) {
      if (source) await source.close();
      throw error;
    }
  }

  async stop(): Promise<void> {
    if (this.startPromise) {
      this.stopRequested = true;
      await this.startPromise;
      return;
    }
    if (this.stopPromise) {
      await this.stopPromise;
      return;
    }
    const active = this.active;
    if (!active) return;
    if (this.deadlineTimer) clearTimeout(this.deadlineTimer);
    this.deadlineTimer = undefined;
    const operation = this.closeActive(active);
    this.stopPromise = operation;
    try {
      await operation;
      if (this.active === active) this.active = undefined;
    } finally {
      if (this.stopPromise === operation) this.stopPromise = undefined;
    }
  }

  private async closeActive(active: ActiveIntercom): Promise<void> {
    const results = await Promise.allSettled([
      Promise.resolve().then(() => active.session.stop()),
      Promise.resolve().then(() => active.source.close()),
    ]);
    const failure = results.find(
      (result): result is PromiseRejectedResult => result.status === 'rejected',
    );
    if (failure) throw failure.reason;
  }

  private validateFormat(format: TalkbackFormat): void {
    if (
      format.codec !== 'pcma' ||
      format.sampleRate !== 8_000 ||
      format.channels !== 1
    ) {
      throw new Error('unsupported talkback format');
    }
  }
}
