function get(obj, path, dflt) {
  let cur = obj;
  for (const key of path.split('.')) {
    cur = cur[key];
  }
  return cur === undefined ? dflt : cur;
}

module.exports = { get };
