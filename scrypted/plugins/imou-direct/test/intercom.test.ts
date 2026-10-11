import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CapabilityGate } from '../src/capabilities';
import {
  IntercomController,
  type EncodedTalkback,
  type TalkbackEncoder,
  type TalkbackTransport,
} from '../src/intercom';

const media = {} as never;

function enabledGate(): CapabilityGate {
  return new CapabilityGate({
    video: false,
    inboundAudio: false,
    doorbellEvents: false,
    intercom: true,
  });
}

function encoded(close = vi.fn(async () => undefined)): EncodedTalkback {
  return {
    format: { codec: 'pcma', sampleRate: 8_000, channels: 1 },
    stream: (async function* () {
      yield new Uint8Array([1]);
    })(),
    close,
  };
}

describe('IntercomController', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('does not touch the microphone or transport when intercom is unverified', async () => {
    const encoder: TalkbackEncoder = { encode: vi.fn() };
    const transport: TalkbackTransport = { start: vi.fn() };
    const controller = new IntercomController(new CapabilityGate(), encoder, transport);

    await expect(controller.start(media)).rejects.toThrowError(
      'intercom is not locally verified',
    );
    expect(encoder.encode).not.toHaveBeenCalled();
    expect(transport.start).not.toHaveBeenCalled();
  });

  it('starts one PCMA 8 kHz session and closes transport and encoder exactly once', async () => {
    const sourceClose = vi.fn(async () => undefined);
    const sessionStop = vi.fn(async () => undefined);
    const encoder: TalkbackEncoder = { encode: vi.fn(async () => encoded(sourceClose)) };
    const transport: TalkbackTransport = {
      start: vi.fn(async () => ({ stop: sessionStop })),
    };
    const controller = new IntercomController(enabledGate(), encoder, transport);

    await controller.start(media);
    await expect(controller.start(media)).rejects.toThrowError(
      'intercom session already active',
    );
    await controller.stop();
    await controller.stop();

    expect(transport.start).toHaveBeenCalledOnce();
    expect(sessionStop).toHaveBeenCalledOnce();
    expect(sourceClose).toHaveBeenCalledOnce();
  });

  it('closes encoded audio if the talk transport fails to start', async () => {
    const sourceClose = vi.fn(async () => undefined);
    const controller = new IntercomController(
      enabledGate(),
      { encode: async () => encoded(sourceClose) },
      { start: async () => { throw new Error('talk transport unavailable'); } },
    );

    await expect(controller.start(media)).rejects.toThrowError(
      'talk transport unavailable',
    );
    expect(sourceClose).toHaveBeenCalledOnce();
  });

  it('automatically stops an abandoned intercom session at its deadline', async () => {
    const sourceClose = vi.fn(async () => undefined);
    const sessionStop = vi.fn(async () => undefined);
    const controller = new IntercomController(
      enabledGate(),
      { encode: async () => encoded(sourceClose) },
      { start: async () => ({ stop: sessionStop }) },
      60_000,
    );

    await controller.start(media);
    await vi.advanceTimersByTimeAsync(60_000);

    expect(sessionStop).toHaveBeenCalledOnce();
    expect(sourceClose).toHaveBeenCalledOnce();
  });

  it('retains a failed deadline cleanup for a later explicit retry', async () => {
    const sessionStop = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(new Error('deadline cleanup failed'))
      .mockResolvedValue(undefined);
    const controller = new IntercomController(
      enabledGate(),
      { encode: async () => encoded() },
      { start: async () => ({ stop: sessionStop }) },
      60_000,
    );

    await controller.start(media);
    await vi.advanceTimersByTimeAsync(60_000);
    await controller.stop();

    expect(sessionStop).toHaveBeenCalledTimes(2);
  });

  it('honors stop requested while the transport is still starting', async () => {
    const sourceClose = vi.fn(async () => undefined);
    const sessionStop = vi.fn(async () => undefined);
    let resolveStart: ((session: { stop(): Promise<void> }) => void) | undefined;
    const startPending = new Promise<{ stop(): Promise<void> }>(resolve => {
      resolveStart = resolve;
    });
    const controller = new IntercomController(
      enabledGate(),
      { encode: async () => encoded(sourceClose) },
      { start: async () => startPending },
    );

    const starting = controller.start(media);
    await Promise.resolve();
    const stopping = controller.stop();
    resolveStart?.({ stop: sessionStop });
    await starting;
    await stopping;

    expect(sessionStop).toHaveBeenCalledOnce();
    expect(sourceClose).toHaveBeenCalledOnce();
  });

  it('attempts encoder cleanup when session stop throws synchronously', async () => {
    const sourceClose = vi.fn(async () => undefined);
    const controller = new IntercomController(
      enabledGate(),
      { encode: async () => encoded(sourceClose) },
      {
        start: async () => ({
          stop: () => {
            throw new Error('session stop failed');
          },
        }),
      },
    );
    await controller.start(media);

    await expect(controller.stop()).rejects.toThrowError('session stop failed');
    expect(sourceClose).toHaveBeenCalledOnce();
  });

  it('blocks a new start while asynchronous stop cleanup is pending', async () => {
    let resolveStop: (() => void) | undefined;
    const stopPending = new Promise<void>(resolve => {
      resolveStop = resolve;
    });
    const controller = new IntercomController(
      enabledGate(),
      { encode: async () => encoded() },
      { start: async () => ({ stop: async () => stopPending }) },
    );
    await controller.start(media);

    const stopping = controller.stop();
    await Promise.resolve();
    await expect(controller.start(media)).rejects.toThrowError(
      'intercom session already active',
    );
    resolveStop?.();
    await stopping;
  });

  it('retains failed cleanup for a later stop retry', async () => {
    const sessionStop = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(new Error('session stop failed'))
      .mockResolvedValue(undefined);
    const controller = new IntercomController(
      enabledGate(),
      { encode: async () => encoded() },
      { start: async () => ({ stop: sessionStop }) },
    );
    await controller.start(media);

    await expect(controller.stop()).rejects.toThrowError('session stop failed');
    await expect(controller.start(media)).rejects.toThrowError(
      'intercom session already active',
    );
    await controller.stop();
    expect(sessionStop).toHaveBeenCalledTimes(2);
  });

  it('does not close encoded audio twice when pending-start cleanup fails', async () => {
    const sourceClose = vi.fn(async () => {
      throw new Error('source close failed');
    });
    let resolveStart: ((session: { stop(): Promise<void> }) => void) | undefined;
    const startPending = new Promise<{ stop(): Promise<void> }>(resolve => {
      resolveStart = resolve;
    });
    const controller = new IntercomController(
      enabledGate(),
      { encode: async () => encoded(sourceClose) },
      { start: async () => startPending },
    );

    const starting = controller.start(media);
    await Promise.resolve();
    const stopping = controller.stop();
    resolveStart?.({ stop: async () => undefined });

    await expect(starting).rejects.toThrowError('source close failed');
    await expect(stopping).rejects.toThrowError('source close failed');
    expect(sourceClose).toHaveBeenCalledOnce();
  });
});
