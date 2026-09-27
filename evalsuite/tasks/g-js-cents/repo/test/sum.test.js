const test = require('node:test');
const assert = require('node:assert');
const { sumPrices } = require('../src/sum');

test('integers', () => assert.strictEqual(sumPrices([1, 2]), 3));
