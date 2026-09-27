function truncate(s, max) {
  if (s.length <= max) return s;
  return s.slice(0, max) + '...';
}

module.exports = { truncate };
