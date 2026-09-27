const test = require('node:test');
const assert = require('node:assert');
const { formatTime } = require('../src/time');

test('two digits already', () => assert.strictEqual(formatTime(754), '12:34'));
