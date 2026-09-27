const test = require('node:test');
const assert = require('node:assert');
const { uniqueBy } = require('../src/unique');

test('distinct', () => assert.strictEqual(uniqueBy([{ id: 1 }, { id: 2 }], 'id').length, 2));
