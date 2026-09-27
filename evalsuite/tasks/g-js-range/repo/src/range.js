/** Inclusive range of integers from start to end. */
function range(start, end) {
  const out = [];
  for (let i = start; i < end; i++) out.push(i);
  return out;
}

module.exports = { range };
