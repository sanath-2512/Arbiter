function parseQuery(qs) {
  const out = {};
  for (const part of qs.replace(/^\?/, '').split('&')) {
    if (!part) continue;
    const [k, v = ''] = part.split('=');
    const dec = (s) => decodeURIComponent(s.replace(/\+/g, ' '));
    out[dec(k)] = dec(v);
  }
  return out;
}

module.exports = { parseQuery };
