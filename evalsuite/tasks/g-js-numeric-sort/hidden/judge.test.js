const test = require('node:test');
const assert = require('node:assert');
const { sortNumbers } = require('../src/sort');

test('judge numeric', () => {
  assert.deepStrictEqual(sortNumbers([10, 9, 1]), [1, 9, 10]);
  assert.deepStrictEqual(sortNumbers([-2, -10, 3]), [-10, -2, 3]);
});
