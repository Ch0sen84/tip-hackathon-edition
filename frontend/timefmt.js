// UX round (2026-10-16), item 6: centralized frontend timestamp
// formatting -- the ONE place every trader-facing page converts a raw
// backend ISO-with-microseconds timestamp (e.g.
// "2026-10-16T14:53:37.980307+00:00") into something a human reads at a
// glance. Backend serialization/storage precision is completely
// unchanged by this file -- this is a DISPLAY-layer-only concern; the
// full-precision value is still exactly what the API returns and what
// the DB stores, this file just never shows it raw to a user.
//
// Deliberately framework-free (no Intl timezone library dependency
// beyond what's built into every real browser's Intl.DateTimeFormat --
// 'America/New_York' is a standard IANA zone name Chrome/Firefox/Safari
// all support natively, DST-correct, with zero extra code or data file
// needed here).
//
// Byte-identical in frontend/ and static-demo/ (same convention as
// app.core.js -- see tools/build_static_demo.sh), loaded as a plain
// <script> before app.core.js/app.live.js so every renderer can call
// its functions directly without an import.

const TIP_ET_ZONE = "America/New_York";

function tipParseTimestamp(raw) {
  if (raw === null || raw === undefined || raw === "") return null;
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return null;
  return d;
}

// formatTimestampET: absolute, human-readable, DST-correct America/
// New_York-zoned form -- "Oct 16, 10:53 AM ET". Returns "Not evaluated"
// for null/undefined/empty/unparsable input (never throws, never
// renders "Invalid Date" or similar raw JS output to a user).
function formatTimestampET(raw) {
  const d = tipParseTimestamp(raw);
  if (!d) return "Not evaluated";
  try {
    const formatted = new Intl.DateTimeFormat("en-US", {
      timeZone: TIP_ET_ZONE, month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit", hour12: true,
    }).format(d);
    return `${formatted} ET`;
  } catch (e) {
    return "Not evaluated"; // malformed Intl input (e.g. truly invalid Date) -- never crash the caller
  }
}

// formatRelative: compact recency string -- "Just updated" (<60s),
// "10h 35m ago", "3d ago", etc. Handles three edge cases explicitly so
// nothing nonsensical (e.g. "-5 minutes ago") ever renders:
//   * null/invalid timestamp     -> "Not evaluated"
//   * a timestamp in the FUTURE  -> "Just updated" (clock-skew-tolerant;
//     never a negative duration) once skew exceeds a small grace window
//     it is reported honestly as a clock-mismatch note instead of a
//     silently-wrong negative value.
//   * a VERY stale timestamp (years old) -> a plain "d ago"/"y ago"
//     form, never an absurd "52560m ago" minute count.
function formatRelative(raw, now) {
  const d = tipParseTimestamp(raw);
  if (!d) return "Not evaluated";
  const nowMs = now instanceof Date ? now.getTime() : (now || Date.now());
  const deltaMs = nowMs - d.getTime();
  const FUTURE_GRACE_MS = 5000; // tolerate small clock skew without reporting "in the future"
  if (deltaMs < -FUTURE_GRACE_MS) {
    return "Timestamp is in the future (check system clock)";
  }
  const deltaS = Math.max(0, deltaMs / 1000);
  if (deltaS < 60) return "Just updated";
  const mins = Math.floor(deltaS / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(deltaS / 3600);
  const remMins = mins % 60;
  if (hours < 24) return remMins > 0 ? `${hours}h ${remMins}m ago` : `${hours}h ago`;
  const days = Math.floor(deltaS / 86400);
  if (days < 365) return `${days}d ago`;
  const years = Math.floor(days / 365);
  return `${years}y ago`;
}

// formatTimestampFull: absolute ET form + relative recency together --
// the common "Oct 16, 10:53 AM ET · 10h 35m ago" pattern used wherever
// space allows both. Falls back to just "Not evaluated" when there is
// no real timestamp at all (never duplicates "Not evaluated" twice).
function formatTimestampFull(raw, now) {
  const d = tipParseTimestamp(raw);
  if (!d) return "Not evaluated";
  return `${formatTimestampET(raw)} \u00b7 ${formatRelative(raw, now)}`;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { formatTimestampET, formatRelative, formatTimestampFull };
}
