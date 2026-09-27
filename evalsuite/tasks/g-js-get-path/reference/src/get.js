function get(obj, path, dflt) {
  let cur = obj;
  for (const key of path.split('.')) {
    if (cur === null || cur === undefined) return dflt;
    cur = cur[key];
  }
  return cur === undefined ? dflt : cur;
}

module.exports = { get };
