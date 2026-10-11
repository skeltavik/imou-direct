export async function closeDeviceResources(
  stopRing: () => void,
  stopIntercom: () => Promise<void>,
): Promise<void> {
  const results = await Promise.allSettled([
    Promise.resolve().then(stopRing),
    Promise.resolve().then(stopIntercom),
  ]);
  const failure = results.find(
    (result): result is PromiseRejectedResult => result.status === 'rejected',
  );
  if (failure) throw failure.reason;
}
