import { createServer, type Socket } from 'node:net';
import { describe, expect, it } from 'vitest';
import {
  DEFAULT_INTERCEPT_PORT,
  InterceptMediaTransport,
} from '../src/intercept-transport';
import { CapabilityGate } from '../src/capabilities';
import { LocalMediaController } from '../src/media';

const ANNEX_B = Buffer.from([0x00, 0x00, 0x00, 0x01]);

function frame(payload: Buffer): Buffer {
  const header = Buffer.alloc(4);
  header.writeUInt32BE(payload.length);
  return Buffer.concat([header, payload]);
}

async function startDaemon(units: Buffer[]): Promise<{ port: number; close(): void }> {
  const server = createServer((client: Socket) => {
    for (const unit of units) client.write(frame(unit));
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('no relay port');
  return { port: address.port, close: () => server.close() };
}

function videoGate(): CapabilityGate {
  return new CapabilityGate({
    video: true,
    inboundAudio: false,
    doorbellEvents: false,
    intercom: false,
  });
}

describe('InterceptMediaTransport', () => {
  it('exposes the default daemon port', () => {
    expect(DEFAULT_INTERCEPT_PORT).toBe(49511);
  });

  it('rejects a missing endpoint without dialing', async () => {
    const transport = new InterceptMediaTransport({ host: '', port: 0 });
    await expect(transport.open()).rejects.toThrowError(
      'intercept daemon endpoint is not configured',
    );
  });

  it('fails fast when no daemon listens', async () => {
    const transport = new InterceptMediaTransport({ host: '127.0.0.1', port: 1 });
    await expect(transport.open()).rejects.toThrowError('intercept daemon is unreachable');
  });

  it('opens a LAN video session from daemon units', async () => {
    const units = [
      Buffer.concat([ANNEX_B, Buffer.from('unit-one')]),
      Buffer.concat([ANNEX_B, Buffer.from('unit-two')]),
    ];
    const daemon = await startDaemon(units);
    try {
      const transport = new InterceptMediaTransport({
        host: '127.0.0.1',
        port: daemon.port,
      });
      const session = await transport.open();
      const inputArguments = session.ffmpegInput.inputArguments;
      if (!inputArguments) throw new Error('missing FFmpeg input');
      try {
        expect(session.origin).toBe('lan');
        expect(session.tracks).toEqual({ video: true, audio: false });
        expect(inputArguments.slice(0, 3)).toEqual(['-f', 'hevc', '-i']);
        expect(inputArguments[3]).toMatch(/^tcp:\/\/127\.0\.0\.1:\d+$/);
      } finally {
        await session.close();
      }
    } finally {
      daemon.close();
    }
  });

  it('passes the controller LAN boundary', async () => {
    const units = [Buffer.concat([ANNEX_B, Buffer.from('unit-one')])];
    const daemon = await startDaemon(units);
    try {
      const transport = new InterceptMediaTransport({
        host: '127.0.0.1',
        port: daemon.port,
      });
      const controller = new LocalMediaController(videoGate(), transport);
      const session = await controller.open();
      try {
        expect(session.origin).toBe('lan');
      } finally {
        await session.close();
      }
    } finally {
      daemon.close();
    }
  });

  it('rejects oversize frames before streaming', async () => {
    const header = Buffer.alloc(4);
    header.writeUInt32BE(9 * 1024 * 1024);
    const server = createServer((client: Socket) => {
      client.write(header);
    });
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
    const address = server.address();
    if (!address || typeof address === 'string') throw new Error('no relay port');
    try {
      const transport = new InterceptMediaTransport({
        host: '127.0.0.1',
        port: address.port,
      });
      await expect(transport.open()).rejects.toThrowError('intercept frame is too large');
    } finally {
      server.close();
    }
  });

  it('rejects non-Annex-B payloads', async () => {
    const daemon = await startDaemon([Buffer.from('XXXX')]);
    try {
      const transport = new InterceptMediaTransport({
        host: '127.0.0.1',
        port: daemon.port,
      });
      await expect(transport.open()).rejects.toThrowError('intercept frame is not Annex-B');
    } finally {
      daemon.close();
    }
  });
});
