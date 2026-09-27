function titleCase(s) {
  return s.split(' ').filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(' ');
}

module.exports = { titleCase };
