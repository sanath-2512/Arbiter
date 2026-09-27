const test = require('node:test');
const assert = require('node:assert');
const { deepEqual } = require('../src/equal');

test('same', () => assert.strictEqual(deepEqual([1], [1]), true));
