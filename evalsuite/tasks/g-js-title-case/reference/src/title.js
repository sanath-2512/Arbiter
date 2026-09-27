function titleCase(s) {
  return s.split(' ').map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w)).join(' ');
}

module.exports = { titleCase };
