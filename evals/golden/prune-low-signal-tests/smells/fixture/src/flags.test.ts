import { existsSync } from 'node:fs';
import { describe, it, expect, vi } from 'vitest';
import * as flags from './flags';
import { render } from './render';

describe('flags', () => {
  it('no longer ships the beta flag file', () => {
    expect(existsSync('src/beta-flag.ts')).toBe(false);
  });

  it('exposes isEnabled', () => {
    expect(typeof flags.isEnabled).toBe('function');
  });

  it('reads the same value twice', () => {
    expect(flags.isEnabled('search')).toEqual(flags.isEnabled('search'));
  });

  it('loads config once', () => {
    const spy = vi.spyOn(flags, 'loadConfig');
    flags.isEnabled('search');
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('caches parsed flags', () => {
    flags.isEnabled('search');
    expect((flags as any).cache.size).toBe(1);
  });

  it.skip('supports the old env var', () => {
    expect(flags.isEnabled('legacy')).toBe(true);
  });

  it('styles the banner', () => {
    expect(render('banner').className).toBe('flex items-center gap-2 px-4 py-2 text-sm');
  });

  it('never leaks the api key into errors', () => {
    expect(() => flags.load({ apiKey: 'sk-123' })).toThrow();
    expect(String(flags.lastError())).not.toContain('sk-123');
  });
});
