const test = require('node:test');
const assert = require('node:assert');
const { titleCase } = require('../src/title');

test('judge spacing', () => {
  assert.strictEqual(titleCase(''), '');
  assert.strictEqual(titleCase('hello   world'), 'Hello   World');
});
