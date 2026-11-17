import { it, expect } from 'vitest';
import { total } from '../totals';

it('sums quantities', () => {
  expect(total([1, 2, 3])).toBe(6);
});

it('is zero for no items', () => {
  expect(total([])).toBe(0);
});
