import { createConnection, createServer, type Server, type Socket } from 'node:net';
import type { FFmpegInput } from '@scrypted/sdk';
import type { MediaSession, MediaTransport } from './media';

export interface InterceptDaemonEndpoint {
  readonly host: string;
  readonly port: number;
}

export const DEFAULT_INTERCEPT_PORT = 49511;

const ANNEX_B_PREFIX = Buffer.from([0x00, 0x00, 0x00, 0x01]);
const MAX_NAL = 8 * 1024 * 1024;

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T | PromiseLike<T>) => void;
  reject: (reason?: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

interface BufferedReader {
  data: Buffer;
}

function validateEndpoint(endpoint: InterceptDaemonEndpoint): void {
  if (!endpoint.host || !endpoint.port || endpoint.port < 1 || endpoint.port > 65_535) {
    throw new Error('intercept daemon endpoint is not configured');
  }
}

function waitForConnect(socket: Socket): Promise<void> {
  const { promise, resolve, reject } = deferred<void>();
  const onConnect = (): void => {
    cleanup();
    resolve();
  };
  const onError = (error: Error): void => {
    cleanup();
    reject(new Error(`intercept daemon is unreachable: ${error.message}`));
  };
  const cleanup = (): void => {
    socket.off('connect', onConnect);
    socket.off('error', onError);
  };
  socket.once('connect', onConnect);
  socket.once('error', onError);
  return promise;
}

function readChunk(socket: Socket): Promise<Buffer | null> {
  const { promise, resolve } = deferred<Buffer | null>();
  const onData = (data: Buffer): void => {
    cleanup();
    resolve(data);
  };
  const onClose = (): void => {
    cleanup();
    resolve(null);
  };
  const onError = (): void => {
    cleanup();
    resolve(null);
  };
  const cleanup = (): void => {
    socket.off('data', onData);
    socket.off('close', onClose);
    socket.off('error', onError);
  };
  socket.once('data', onData);
  socket.once('close', onClose);
  socket.once('error', onError);
  return promise;
}

async function readExact(
  socket: Socket,
  buffered: BufferedReader,
  n: number,
): Promise<Buffer> {
  while (buffered.data.length < n) {
    const chunk = await readChunk(socket);
    if (chunk === null) throw new Error('intercept daemon closed the stream');
    buffered.data = Buffer.concat([buffered.data, chunk]);
  }
  const out = buffered.data.subarray(0, n);
  buffered.data = buffered.data.subarray(n);
  return Buffer.from(out);
}

async function readUnit(socket: Socket, buffered: BufferedReader): Promise<Buffer> {
  const sizeBytes = await readExact(socket, buffered, 4);
  const size = sizeBytes.readUInt32BE(0);
  if (size <= 0 || size > MAX_NAL) {
    throw new Error('intercept frame is too large');
  }
  const unit = await readExact(socket, buffered, size);
  if (!unit.subarray(0, 4).equals(ANNEX_B_PREFIX)) {
    throw new Error('intercept frame is not Annex-B');
  }
  return unit;
}

function waitForClient(relay: Server): Promise<Socket> {
  const { promise, resolve, reject } = deferred<Socket>();
  relay.once('connection', (client: Socket) => {
    relay.close();
    resolve(client);
  });
  relay.once('error', (error: Error) => {
    reject(error);
  });
  return promise;
}

function relayAddress(relay: Server): number {
  const address = relay.address();
  if (!address || typeof address === 'string') {
    throw new Error('intercept relay failed to bind');
  }
  return address.port;
}

/**
 * LAN media transport that proxies Annex-B NAL units from the statically
 * configured imou-intercept daemon (length-prefixed TCP framing) to
 * Scrypted's FFmpeg over a loopback TCP relay. No cloud, no app.
 *
 * The daemon speaks length-prefixed TCP while Scrypted dials its own
 * input URL, so this transport bridges the two: the primed first unit
 * plus any buffered bytes are replayed to FFmpeg before live bytes
 * stream through untouched.
 */
export class InterceptMediaTransport implements MediaTransport {
  constructor(private readonly endpoint: InterceptDaemonEndpoint) {}

  async open(): Promise<MediaSession> {
    validateEndpoint(this.endpoint);
    const upstream = createConnection({
      host: this.endpoint.host,
      port: this.endpoint.port,
    });
    try {
      await waitForConnect(upstream);
    } catch (error) {
      upstream.destroy();
      throw error;
    }

    const buffered: BufferedReader = { data: Buffer.alloc(0) };
    let first: Buffer;
    try {
      first = await readUnit(upstream, buffered);
    } catch (error) {
      upstream.destroy();
      throw error;
    }

    const relay = createServer();
    const clientPromise = waitForClient(relay);
    relay.on('error', () => {
      upstream.destroy();
    });
    await new Promise<void>((resolve, reject) => {
      relay.once('listening', () => resolve());
      relay.once('error', (error: Error) => reject(error));
      relay.listen(0, '127.0.0.1');
    }).catch((error: Error) => {
      upstream.destroy();
      relay.close();
      throw error;
    });
    let relayPort: number;
    try {
      relayPort = relayAddress(relay);
    } catch (error) {
      upstream.destroy();
      relay.close();
      throw error;
    }

    let closed = false;
    const session: MediaSession = {
      origin: 'lan',
      tracks: { video: true, audio: false },
      ffmpegInput: {
        inputArguments: ['-f', 'hevc', '-i', `tcp://127.0.0.1:${relayPort}`],
        mediaStreamOptions: {
          id: 'intercept-main',
          name: 'Intercept Main Stream',
          source: 'local',
          container: 'rawvideo',
          tool: 'ffmpeg',
          video: { codec: 'hevc' },
        },
      },
      close: async () => {
        closed = true;
        upstream.destroy();
        relay.close();
      },
    };

    void (async () => {
      try {
        const client = await clientPromise;
        try {
          client.write(first);
          if (buffered.data.length) client.write(buffered.data);
          upstream.on('data', (chunk: Buffer) => {
            if (!client.destroyed) client.write(chunk);
          });
          await new Promise<void>(resolveDownstream => {
            upstream.once('close', () => resolveDownstream());
            upstream.once('error', () => resolveDownstream());
            client.once('close', () => resolveDownstream());
            client.once('error', () => resolveDownstream());
          });
          client.destroy();
        } catch {
          client.destroy();
        }
      } catch {
        upstream.destroy();
      } finally {
        if (!closed) upstream.destroy();
      }
    })();

    return session;
  }
}
