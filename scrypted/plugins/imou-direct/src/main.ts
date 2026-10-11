import sdk, {
  type BinarySensor,
  type Device,
  type DeviceProvider,
  type Intercom,
  type MediaObject,
  type RequestMediaStreamOptions,
  type ResponseMediaStreamOptions,
  ScryptedDeviceBase,
  ScryptedDeviceType,
  ScryptedInterface,
  type ScryptedNativeId,
  type Setting,
  type Settings,
  type SettingValue,
  type VideoCamera,
} from '@scrypted/sdk';
import { CapabilityGate, type CapabilitySnapshot } from './capabilities';
import { createDoorbellDescriptor } from './device-descriptor';
import { closeDeviceResources } from './device-lifecycle';
import {
  IntercomController,
  type TalkbackEncoder,
  type TalkbackTransport,
} from './intercom';
import { LocalMediaController, type MediaTransport } from './media';
import {
  DEFAULT_INTERCEPT_PORT,
  InterceptMediaTransport,
} from './intercept-transport';
import { RingEventController, type RingEventSource } from './ring-events';

const NATIVE_ID = 'imou-doorbell';
const INTERCEPT_HOST_KEY = 'interceptHost';
const INTERCEPT_PORT_KEY = 'interceptPort';
const DEFAULT_INTERCEPT_HOST = '127.0.0.1';

// Video is locally verified: the intercept daemon taps the Chime's own
// upload stream on the LAN and serves Annex-B HEVC with no cloud or app.
// Live evidence: 11-10-2026 daemon-to-transport pipeline decoded 1920x1920
// HEVC with same-day OSD from an operator-captured upload session.
// Audio, doorbell events, and intercom remain closed until their own gates.
const VERIFIED_CAPABILITIES: CapabilitySnapshot = Object.freeze({
  video: true,
  inboundAudio: false,
  doorbellEvents: false,
  intercom: false,
});

class UnavailableRingSource implements RingEventSource {
  subscribe(): () => void {
    throw new Error('local doorbell event transport is not implemented');
  }
}

class UnavailableMediaTransport implements MediaTransport {
  async open(): Promise<never> {
    throw new Error('local media transport is not implemented');
  }
}

class UnavailableTalkbackEncoder implements TalkbackEncoder {
  async encode(): Promise<never> {
    throw new Error('talkback encoder is not implemented');
  }
}

class UnavailableTalkbackTransport implements TalkbackTransport {
  async start(): Promise<never> {
    throw new Error('local talkback transport is not implemented');
  }
}

export class ImouDoorbellDevice
  extends ScryptedDeviceBase
  implements VideoCamera, BinarySensor, Intercom, Settings
{
  private readonly gate: CapabilityGate;
  private readonly ring: RingEventController;
  private media: LocalMediaController;
  private readonly intercom: IntercomController;
  private interceptHost = DEFAULT_INTERCEPT_HOST;
  private interceptPort = DEFAULT_INTERCEPT_PORT;

  constructor(
    nativeId: string,
    capabilities: CapabilitySnapshot = VERIFIED_CAPABILITIES,
    ringSource: RingEventSource = new UnavailableRingSource(),
    mediaTransport: MediaTransport | null = null,
    talkbackEncoder: TalkbackEncoder = new UnavailableTalkbackEncoder(),
    talkbackTransport: TalkbackTransport = new UnavailableTalkbackTransport(),
  ) {
    super(nativeId);
    this.gate = new CapabilityGate(capabilities);
    this.binaryState = false;
    this.online = false;
    this.ring = new RingEventController(
      this.gate,
      ringSource,
      active => {
        this.binaryState = active;
      },
      1_000,
    );
    this.media = new LocalMediaController(
      this.gate,
      mediaTransport ??
        new InterceptMediaTransport({
          host: this.interceptHost,
          port: this.interceptPort,
        }),
    );
    this.intercom = new IntercomController(
      this.gate,
      talkbackEncoder,
      talkbackTransport,
    );

    if (capabilities.doorbellEvents) this.ring.start();
  }

  async getSettings(): Promise<Setting[]> {
    const capabilities = this.gate.snapshot();
    const evidence: Setting[] = Object.entries(capabilities).map(([key, value]) => ({
      key: `evidence-${key}`,
      title: `${key} locally verified`,
      description: 'Read-only build evidence gate; not a user override.',
      readonly: true,
      type: 'boolean' as const,
      value,
    }));
    return [
      {
        key: INTERCEPT_HOST_KEY,
        title: 'Intercept daemon host',
        description:
          'LAN address of the imou-intercept add-on (no default device address).',
        type: 'string' as const,
        value: this.interceptHost,
      },
      {
        key: INTERCEPT_PORT_KEY,
        title: 'Intercept daemon port',
        description: 'Annex-B feed port of the imou-intercept add-on.',
        type: 'number' as const,
        value: this.interceptPort,
      },
      ...evidence,
    ];
  }

  async putSetting(key: string, value: SettingValue): Promise<void> {
    if (key === INTERCEPT_HOST_KEY && typeof value === 'string' && value) {
      this.interceptHost = value;
      this.media = new LocalMediaController(
        this.gate,
        new InterceptMediaTransport({ host: value, port: this.interceptPort }),
      );
      return;
    }
    if (key === INTERCEPT_PORT_KEY && typeof value === 'number') {
      if (!Number.isInteger(value) || value < 1 || value > 65_535) {
        throw new Error('intercept port is invalid');
      }
      this.interceptPort = value;
      this.media = new LocalMediaController(
        this.gate,
        new InterceptMediaTransport({ host: this.interceptHost, port: value }),
      );
      return;
    }
    throw new Error('evidence gates are read-only');
  }

  async getVideoStreamOptions(): Promise<ResponseMediaStreamOptions[]> {
    this.gate.require('video');
    return [
      {
        id: 'local-main',
        name: 'Local Main Stream',
        source: 'local',
        container: 'rawvideo',
        tool: 'ffmpeg',
        video: { codec: 'hevc' },
        audio: this.gate.snapshot().inboundAudio
          ? { codec: 'pcm_alaw', sampleRate: 16_000 }
          : undefined,
      },
    ];
  }

  async getVideoStream(
    _options?: RequestMediaStreamOptions,
  ): Promise<MediaObject> {
    const session = await this.media.open();
    try {
      return await sdk.mediaManager.createFFmpegMediaObject(session.ffmpegInput);
    } catch (error) {
      await session.close();
      throw error;
    }
  }

  async startIntercom(media: MediaObject): Promise<void> {
    await this.intercom.start(media);
  }

  async stopIntercom(): Promise<void> {
    await this.intercom.stop();
  }

  async close(): Promise<void> {
    await closeDeviceResources(
      () => this.ring.stop(),
      () => this.intercom.stop(),
    );
  }
}

export class ImouDirectProvider extends ScryptedDeviceBase implements DeviceProvider {
  private readonly device: ImouDoorbellDevice;

  constructor(nativeId?: string) {
    super(nativeId);
    this.device = new ImouDoorbellDevice(NATIVE_ID);
    void this.syncDevice().catch(() => {
      // Scrypted will surface provider startup failure. Do not log private state.
    });
  }

  private async syncDevice(): Promise<void> {
    const descriptor = createDoorbellDescriptor(
      NATIVE_ID,
      new CapabilityGate(VERIFIED_CAPABILITIES),
    );
    const device: Device = {
      nativeId: descriptor.nativeId,
      name: descriptor.name,
      type: ScryptedDeviceType.Doorbell,
      interfaces: descriptor.interfaces as ScryptedInterface[],
    };
    await sdk.deviceManager.onDevicesChanged({ devices: [device] });
  }

  async getDevice(nativeId: ScryptedNativeId): Promise<ImouDoorbellDevice> {
    if (nativeId !== NATIVE_ID) throw new Error('unknown device');
    return this.device;
  }

  async releaseDevice(_id: string, nativeId: ScryptedNativeId): Promise<void> {
    if (nativeId !== NATIVE_ID) return;
    await this.device.close();
  }
}

export default new ImouDirectProvider();
