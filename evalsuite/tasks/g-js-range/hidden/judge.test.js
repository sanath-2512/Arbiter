const test = require('node:test');
const assert = require('node:assert');
const { range } = require('../src/range');

test('judge inclusive', () => {
  assert.deepStrictEqual(range(1, 5), [1, 2, 3, 4, 5]);
  assert.deepStrictEqual(range(3, 3), [3]);
});
