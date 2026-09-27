const test = require('node:test');
const assert = require('node:assert');
const { uniqueBy } = require('../src/unique');

test('judge first wins', () => {
  const r = uniqueBy([{ id: 1, n: 'a' }, { id: 2, n: 'b' }, { id: 1, n: 'c' }], 'id');
  assert.deepStrictEqual(r.map((x) => x.n), ['a', 'b']);
});
