const test = require('node:test');
const assert = require('node:assert');
const { parseQuery } = require('../src/query');

test('judge decode', () => {
  assert.deepStrictEqual(parseQuery('?q=hello%20world&tag=a+b'), { q: 'hello world', tag: 'a b' });
});
