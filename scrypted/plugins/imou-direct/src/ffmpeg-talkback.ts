import type { FFmpegInput, MediaObject } from '@scrypted/sdk';
import type { EncodedTalkback, TalkbackEncoder } from './intercom';

export interface FfmpegProcess {
  readonly output: AsyncIterable<Uint8Array>;
  stop(): Promise<void>;
}

export interface FfmpegProcessFactory {
  start(arguments_: readonly string[]): FfmpegProcess;
}

export type FfmpegInputConverter = (
  media: MediaObject,
) => Promise<FFmpegInput>;

export class FfmpegTalkbackEncoder implements TalkbackEncoder {
  constructor(
    private readonly convert: FfmpegInputConverter,
    private readonly processes: FfmpegProcessFactory,
  ) {}

  async encode(media: MediaObject): Promise<EncodedTalkback> {
    const input = await this.convert(media);
    if (!input.inputArguments?.length) {
      throw new Error('microphone media has no FFmpeg input');
    }
    const process = this.processes.start([
      '-hide_banner',
      '-loglevel',
      'error',
      ...input.inputArguments,
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
    let closed = false;

    return {
      format: { codec: 'pcma', sampleRate: 8_000, channels: 1 },
      stream: process.output,
      async close(): Promise<void> {
        if (closed) return;
        closed = true;
        await process.stop();
      },
    };
  }
}
