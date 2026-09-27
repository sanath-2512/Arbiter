const test = require('node:test');
const assert = require('node:assert');
const { range } = require('../src/range');

test('length', () => assert.ok(range(0, 3).length >= 3));
