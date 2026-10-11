import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

describe('Scrypted entrypoint', () => {
  it('exports an instantiated provider for the plugin host', async () => {
    const source = await readFile(resolve('src/main.ts'), 'utf8');

    expect(source).toContain('export default new ImouDirectProvider();');
  });
});
