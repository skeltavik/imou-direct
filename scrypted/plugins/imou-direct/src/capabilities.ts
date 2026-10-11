export type LocalCapability =
  | 'video'
  | 'inboundAudio'
  | 'doorbellEvents'
  | 'intercom';

export type CapabilitySnapshot = Readonly<Record<LocalCapability, boolean>>;

const DISABLED_CAPABILITIES: CapabilitySnapshot = Object.freeze({
  video: false,
  inboundAudio: false,
  doorbellEvents: false,
  intercom: false,
});

export class CapabilityGate {
  private readonly capabilities: CapabilitySnapshot;

  constructor(capabilities: CapabilitySnapshot = DISABLED_CAPABILITIES) {
    this.capabilities = Object.freeze({ ...capabilities });
  }

  snapshot(): CapabilitySnapshot {
    return { ...this.capabilities };
  }

  require(capability: LocalCapability): void {
    if (!this.capabilities[capability]) {
      throw new Error(`${capability} is not locally verified`);
    }
  }
}
