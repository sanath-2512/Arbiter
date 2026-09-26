'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { parseDuration } = require('../src/duration');

test('single unit', () => {
  assert.strictEqual(parseDuration('45s'), 45);
  assert.strictEqual(parseDuration('10m'), 600);
});
