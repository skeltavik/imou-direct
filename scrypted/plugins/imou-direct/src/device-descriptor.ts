import { CapabilityGate } from './capabilities';

export interface DoorbellDescriptor {
  readonly nativeId: string;
  readonly name: string;
  readonly type: 'Doorbell';
  readonly interfaces: string[];
}

export function createDoorbellDescriptor(
  nativeId: string,
  gate: CapabilityGate,
): DoorbellDescriptor {
  const capabilities = gate.snapshot();
  const interfaces = ['Settings', 'Online'];
  if (capabilities.video) interfaces.push('VideoCamera');
  if (capabilities.doorbellEvents) interfaces.push('BinarySensor');
  if (capabilities.intercom) interfaces.push('Intercom');

  return {
    nativeId,
    name: 'Imou Doorbell',
    type: 'Doorbell',
    interfaces,
  };
}
