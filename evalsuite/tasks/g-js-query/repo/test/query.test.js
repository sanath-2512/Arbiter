const test = require('node:test');
const assert = require('node:assert');
const { parseQuery } = require('../src/query');

test('plain', () => assert.deepStrictEqual(parseQuery('a=1'), { a: '1' }));
