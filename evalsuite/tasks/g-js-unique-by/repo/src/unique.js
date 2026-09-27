function uniqueBy(items, key) {
  const m = new Map();
  for (const it of items) m.set(it[key], it);
  return [...m.values()];
}

module.exports = { uniqueBy };
