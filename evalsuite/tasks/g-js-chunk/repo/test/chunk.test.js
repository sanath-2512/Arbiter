const test = require('node:test');
const assert = require('node:assert');
const { chunk } = require('../src/chunk');

test('even', () => assert.deepStrictEqual(chunk([1, 2, 3, 4], 2), [[1, 2], [3, 4]]));
