'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { parseDuration } = require('../src/duration');

test('compound', () => {
  assert.strictEqual(parseDuration('1h30m'), 5400);
  assert.strictEqual(parseDuration('2m5s'), 125);
  assert.strictEqual(parseDuration('1h2m3s'), 3723);
});

test('single', () => {
  assert.strictEqual(parseDuration('45s'), 45);
  assert.strictEqual(parseDuration('2h'), 7200);
});

test('invalid', () => {
  for (const bad of ['', '10', '5x', '1h foo', 'h', 'abc']) {
    assert.throws(() => parseDuration(bad), Error, bad);
  }
});
