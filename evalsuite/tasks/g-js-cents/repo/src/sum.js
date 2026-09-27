function sumPrices(prices) {
  return prices.reduce((a, b) => a + b, 0);
}

module.exports = { sumPrices };
