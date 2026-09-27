const test = require('node:test');
const assert = require('node:assert');
const { deepEqual } = require('../src/equal');

test('judge lengths', () => {
  assert.strictEqual(deepEqual([1, 2], [1, 2, 3]), false);
  assert.strictEqual(deepEqual({ a: 1 }, { a: 1, b: 2 }), false);
  assert.strictEqual(deepEqual({ a: [1] }, { a: [1] }), true);
});
