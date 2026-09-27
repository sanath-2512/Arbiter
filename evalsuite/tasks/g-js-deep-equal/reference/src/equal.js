function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== 'object' || typeof b !== 'object' || !a || !b) return false;
  if (Object.keys(a).length !== Object.keys(b).length) return false;
  for (const k of Object.keys(a)) {
    if (!deepEqual(a[k], b[k])) return false;
  }
  return true;
}

module.exports = { deepEqual };
