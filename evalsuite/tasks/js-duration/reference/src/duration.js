'use strict';

const UNIT_SECONDS = { h: 3600, m: 60, s: 1 };

/**
 * Parse a duration such as "45s", "10m" or "1h30m" into seconds.
 */
function parseDuration(text) {
  const match = /^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$/.exec(text);
  if (!match || text === '') {
    throw new Error(`invalid duration: ${JSON.stringify(text)}`);
  }
  const [, h, m, s] = match;
  return Number(h || 0) * UNIT_SECONDS.h + Number(m || 0) * UNIT_SECONDS.m + Number(s || 0) * UNIT_SECONDS.s;
}

module.exports = { parseDuration };
