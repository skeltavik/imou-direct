import { describe, expect, it, vi } from 'vitest';
import type { FFmpegInput, MediaObject } from '@scrypted/sdk';
import {
  FfmpegTalkbackEncoder,
  type FfmpegProcessFactory,
} from '../src/ffmpeg-talkback';

const media = {} as MediaObject;

describe('FfmpegTalkbackEncoder', () => {
  it('converts Scrypted microphone media to PCMA 8 kHz mono', async () => {
    const stop = vi.fn(async () => undefined);
    const output = (async function* () {
      yield new Uint8Array([1, 2]);
    })();
    const factory: FfmpegProcessFactory = {
      start: vi.fn(() => ({ output, stop })),
    };
    const input: FFmpegInput = {
      inputArguments: ['-f', 's16le', '-i', 'pipe:0'],
    };
    const encoder = new FfmpegTalkbackEncoder(
      async value => {
        expect(value).toBe(media);
        return input;
      },
      factory,
    );

    const encoded = await encoder.encode(media);

    expect(factory.start).toHaveBeenCalledWith([
      '-hide_banner',
      '-loglevel',
      'error',
      '-f',
      's16le',
      '-i',
      'pipe:0',
      '-vn',
      '-acodec',
      'pcm_alaw',
      '-ac',
      '1',
      '-ar',
      '8000',
      '-f',
      'alaw',
      'pipe:3',
    ]);
    expect(encoded.format).toEqual({
      codec: 'pcma',
      sampleRate: 8_000,
      channels: 1,
    });
    const chunks: Uint8Array[] = [];
    for await (const chunk of encoded.stream) chunks.push(chunk);
    expect(chunks).toEqual([new Uint8Array([1, 2])]);
    await encoded.close();
    await encoded.close();
    expect(stop).toHaveBeenCalledOnce();
  });
});
