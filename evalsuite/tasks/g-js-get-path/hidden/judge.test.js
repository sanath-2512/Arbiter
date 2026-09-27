const test = require('node:test');
const assert = require('node:assert');
const { get } = require('../src/get');

test('judge missing', () => {
  assert.strictEqual(get({ a: {} }, 'a.b.c'), undefined);
  assert.strictEqual(get({}, 'x.y', 7), 7);
  assert.strictEqual(get({ a: { b: 0 } }, 'a.b', 5), 0);
});
