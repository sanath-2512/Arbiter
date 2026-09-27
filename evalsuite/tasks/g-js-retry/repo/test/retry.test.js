const test = require('node:test');
const assert = require('node:assert');
const { retry } = require('../src/retry');

test('succeeds at once', async () => assert.strictEqual(await retry(async () => 1, 3), 1));
