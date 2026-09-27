const test = require('node:test');
const assert = require('node:assert');
const { truncate } = require('../src/truncate');

test('judge limit', () => {
  assert.strictEqual(truncate('hello world', 8), 'hello...');
  assert.ok(truncate('abcdefghijk', 5).length <= 5);
});
