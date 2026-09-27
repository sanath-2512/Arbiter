function uniqueBy(items, key) {
  const m = new Map();
  for (const it of items) if (!m.has(it[key])) m.set(it[key], it);
  return [...m.values()];
}

module.exports = { uniqueBy };
