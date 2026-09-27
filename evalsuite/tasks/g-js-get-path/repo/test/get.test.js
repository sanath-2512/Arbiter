const test = require('node:test');
const assert = require('node:assert');
const { get } = require('../src/get');

test('present', () => assert.strictEqual(get({ a: { b: 2 } }, 'a.b'), 2));
