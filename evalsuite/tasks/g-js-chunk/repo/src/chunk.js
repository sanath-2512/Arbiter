function chunk(xs, size) {
  const out = [];
  for (let i = 0; i + size <= xs.length; i += size) {
    out.push(xs.slice(i, i + size));
  }
  return out;
}

module.exports = { chunk };
