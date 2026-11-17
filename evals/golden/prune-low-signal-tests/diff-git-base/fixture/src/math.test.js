const { add } = require('./math');
const { oldRound } = require('./oldRound');

test('adds two numbers', () => {
  expect(add(2, 3)).toBe(5);
});

test('oldRound still rounds down', () => {
  expect(oldRound(2.7)).toBe(2);
});
