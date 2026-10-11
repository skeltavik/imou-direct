import type { FFmpegInput } from '@scrypted/sdk';
import { CapabilityGate } from './capabilities';

export type MediaOrigin = 'lan' | 'vendor-assisted';

export interface MediaSession {
  readonly origin: MediaOrigin;
  readonly tracks: Readonly<{ video: boolean; audio: boolean }>;
  readonly ffmpegInput: FFmpegInput;
  close(): Promise<void>;
}

export interface MediaTransport {
  open(): Promise<MediaSession>;
}

export class LocalMediaController {
  constructor(
    private readonly gate: CapabilityGate,
    private readonly transport: MediaTransport,
  ) {}

  async open(): Promise<MediaSession> {
    this.gate.require('video');
    const session = await this.transport.open();
    try {
      if (session.origin !== 'lan') {
        throw new Error('media session is not LAN-local');
      }
      if (!session.tracks.video) {
        throw new Error('media session contains no video track');
      }
      if (session.tracks.audio) {
        this.gate.require('inboundAudio');
      }
      return session;
    } catch (error) {
      await session.close();
      throw error;
    }
  }
}
