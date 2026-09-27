const test = require('node:test');
const assert = require('node:assert');
const { sortNumbers } = require('../src/sort');

test('digits', () => assert.deepStrictEqual(sortNumbers([3, 1, 2]), [1, 2, 3]));
