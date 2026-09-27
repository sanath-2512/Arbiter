const test = require('node:test');
const assert = require('node:assert');
const { retry } = require('../src/retry');

test('judge attempts', async () => {
  let calls = 0;
  const v = await retry(async () => { calls++; if (calls < 3) throw new Error('x'); return 'ok'; }, 3);
  assert.strictEqual(v, 'ok');
  assert.strictEqual(calls, 3);
});
