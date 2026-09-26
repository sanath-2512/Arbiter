'use strict';

const UNIT_SECONDS = { h: 3600, m: 60, s: 1 };

/**
 * Parse a duration such as "45s", "10m" or "1h30m" into seconds.
 */
function parseDuration(text) {
  const match = /(\d+)([hms])/.exec(text);
  if (!match) {
    throw new Error(`invalid duration: ${JSON.stringify(text)}`);
  }
  return Number(match[1]) * UNIT_SECONDS[match[2]];
}

module.exports = { parseDuration };
