/** The website emits UTC with six or more fractional digits. */
const websiteTimestamp = /^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})\.([0-9]{6,})Z$/;

function websiteInstant(value) {
  if (typeof value !== 'string') return null;
  const match = websiteTimestamp.exec(value);
  if (!match || value.startsWith('0000')) return null;
  const whole = match[1] + '.000Z';
  const ms = Date.parse(whole);
  // Date.parse normalizes some invalid dates and 24:00 to the next day.
  if (!Number.isFinite(ms) || new Date(ms).toISOString() !== whole) return null;
  return {seconds: ms / 1000, fraction: match[2].replace(/0+$/, '')};
}

export function isWebsiteTime(value) {
  return websiteInstant(value) !== null;
}

export function formatWebsiteTime(value, missing = '时间未提供') {
  const instant = websiteInstant(value);
  if (!instant) return missing;
  const ms = Number(instant.fraction.slice(0, 3).padEnd(3, '0'));
  return new Date(instant.seconds * 1000 + ms).toLocaleString('zh-CN');
}

function compareInstants(a, b) {
  if (!a || !b) return Number(Boolean(a)) - Number(Boolean(b));
  if (a.seconds !== b.seconds) return a.seconds - b.seconds;
  if (a.fraction === b.fraction) return 0;
  return a.fraction < b.fraction ? -1 : 1;
}

export function compareWebsiteTimes(left, right) {
  return compareInstants(websiteInstant(left), websiteInstant(right));
}

const decimal = new Intl.NumberFormat('en-US', {useGrouping: false, maximumSignificantDigits: 21});
function researchInstant(seconds) {
  if (!Number.isFinite(seconds)) return null;
  // Expand exponent notation without rounding or subtracting a large epoch from the float.
  let fraction = decimal.format(Math.abs(seconds)).split('.')[1] || '';
  if (seconds < 0 && fraction) {
    const scale = 10n ** BigInt(fraction.length);
    fraction = (scale - BigInt(fraction)).toString().padStart(fraction.length, '0');
  }
  return {seconds: Math.floor(seconds), fraction};
}

export function compareChatTimes(left, right) {
  const instant = chat => chat.kind === 'research' ? researchInstant(chat.updated_at) : websiteInstant(chat.updated_at);
  return compareInstants(instant(left), instant(right));
}
