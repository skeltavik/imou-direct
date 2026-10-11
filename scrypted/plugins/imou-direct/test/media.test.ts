import { describe, expect, it, vi } from 'vitest';
import { CapabilityGate } from '../src/capabilities';
import {
  LocalMediaController,
  type MediaSession,
  type MediaTransport,
} from '../src/media';

function capabilities(inboundAudio: boolean): CapabilityGate {
  return new CapabilityGate({
    video: true,
    inboundAudio,
    doorbellEvents: false,
    intercom: false,
  });
}

describe('LocalMediaController', () => {
  it('refuses all media when local video is unverified', async () => {
    const transport: MediaTransport = { open: vi.fn() };
    const controller = new LocalMediaController(new CapabilityGate(), transport);

    await expect(controller.open()).rejects.toThrowError(
      'video is not locally verified',
    );
    expect(transport.open).not.toHaveBeenCalled();
  });

  it('closes and rejects a public or vendor-assisted media session', async () => {
    const close = vi.fn(async () => undefined);
    const session: MediaSession = {
      origin: 'vendor-assisted',
      tracks: { video: true, audio: false },
      ffmpegInput: { inputArguments: ['-i', 'redacted-source'] },
      close,
    };
    const controller = new LocalMediaController(capabilities(false), {
      open: async () => session,
    });

    await expect(controller.open()).rejects.toThrowError(
      'media session is not LAN-local',
    );
    expect(close).toHaveBeenCalledOnce();
  });

  it('requires independent inbound-audio evidence before exposing audio', async () => {
    const close = vi.fn(async () => undefined);
    const session: MediaSession = {
      origin: 'lan',
      tracks: { video: true, audio: true },
      ffmpegInput: { inputArguments: ['-i', 'redacted-source'] },
      close,
    };
    const controller = new LocalMediaController(capabilities(false), {
      open: async () => session,
    });

    await expect(controller.open()).rejects.toThrowError(
      'inboundAudio is not locally verified',
    );
    expect(close).toHaveBeenCalledOnce();
  });

  it('returns a verified LAN session unchanged', async () => {
    const session: MediaSession = {
      origin: 'lan',
      tracks: { video: true, audio: true },
      ffmpegInput: { inputArguments: ['-f', 'hevc', '-i', 'pipe:3'] },
      close: async () => undefined,
    };
    const controller = new LocalMediaController(capabilities(true), {
      open: async () => session,
    });

    await expect(controller.open()).resolves.toBe(session);
  });
});
