const test = require('node:test');
const assert = require('node:assert');
const { chunk } = require('../src/chunk');

test('judge remainder', () => {
  assert.deepStrictEqual(chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]);
  assert.deepStrictEqual(chunk([], 3), []);
});
