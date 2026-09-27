const test = require('node:test');
const assert = require('node:assert');
const { formatTime } = require('../src/time');

test('judge padding', () => {
  assert.strictEqual(formatTime(65), '01:05');
  assert.strictEqual(formatTime(0), '00:00');
  assert.strictEqual(formatTime(754), '12:34');
});
