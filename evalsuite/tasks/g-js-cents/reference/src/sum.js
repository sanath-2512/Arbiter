function sumPrices(prices) {
  return Math.round(prices.reduce((a, b) => a + b, 0) * 100) / 100;
}

module.exports = { sumPrices };
