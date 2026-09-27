function truncate(s, max) {
  if (s.length <= max) return s;
  return s.slice(0, Math.max(0, max - 3)) + '...';
}

module.exports = { truncate };
