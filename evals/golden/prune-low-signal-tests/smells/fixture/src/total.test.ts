import { it, expect } from 'vitest';
import { total } from './total';

it('sums prices times quantities', () => {
  expect(total([{ price: 2, qty: 3 }, { price: 1, qty: 1 }])).toBe(7);
});

it('rejects a negative quantity', () => {
  expect(() => total([{ price: 2, qty: -1 }])).toThrow('quantity');
});
