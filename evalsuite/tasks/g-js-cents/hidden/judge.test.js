const test = require('node:test');
const assert = require('node:assert');
const { sumPrices } = require('../src/sum');

test('judge cents', () => {
  assert.strictEqual(sumPrices([0.1, 0.2]), 0.3);
  assert.strictEqual(sumPrices([1.25, 2.1, 0.7]), 4.05);
});
