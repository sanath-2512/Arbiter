const test = require('node:test');
const assert = require('node:assert');
const { truncate } = require('../src/truncate');

test('short', () => assert.strictEqual(truncate('hi', 5), 'hi'));
