export function placeOrder(input: { sku: string; qty: number }) {
  if (input.qty <= 0) throw new Error('quantity must be positive');
  return { id: `ord_${input.sku}`, sku: input.sku };
}
