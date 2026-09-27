async function retry(fn, attempts) {
  let last;
  for (let i = 1; i < attempts; i++) {
    try {
      return await fn();
    } catch (e) {
      last = e;
    }
  }
  throw last;
}

module.exports = { retry };
