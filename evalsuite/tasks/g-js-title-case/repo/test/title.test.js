const test = require('node:test');
const assert = require('node:assert');
const { titleCase } = require('../src/title');

test('two words', () => assert.strictEqual(titleCase('a b'), 'A B'));
