function parseQuery(qs) {
  const out = {};
  for (const part of qs.replace(/^\?/, '').split('&')) {
    if (!part) continue;
    const [k, v = ''] = part.split('=');
    out[k] = v;
  }
  return out;
}

module.exports = { parseQuery };
