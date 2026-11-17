import { describe, it, expect } from 'vitest';
import { placeOrder } from './orders';

describe('placeOrder', () => {
  it('returns an order id', () => {
    expect(placeOrder({ sku: 'a', qty: 1 }).id).toMatch(/^ord_/);
  });

  it('rejects a zero quantity', () => {
    expect(() => placeOrder({ sku: 'a', qty: 0 })).toThrow('quantity');
  });

  test('keeps the sku', () => {
    expect(placeOrder({ sku: 'b', qty: 2 }).sku).toBe('b');
  });
});
