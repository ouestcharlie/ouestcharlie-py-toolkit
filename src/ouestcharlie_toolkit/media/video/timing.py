"""Video capture time and location from container metadata tags."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta, timezone, tzinfo

from ouestcharlie_toolkit.media.filename_time import date_from_filename, datetime_from_filename

_log = logging.getLogger(__name__)


def container_tag(metadata: dict[str, str], *keys: str) -> str | None:
    """Return the first non-empty value among *keys* in a container metadata dict."""
    for key in keys:
        val = (metadata.get(key) or "").strip()
        if val:
            return val
    return None


def _parse_creation_time(raw: str | None) -> datetime | None:
    """Parse a container ``creation_time`` tag (ISO-8601, often trailing 'Z')."""
    if not raw:
        return None
    try:
        # Python's fromisoformat handles the trailing 'Z' since 3.11.
        return datetime.fromisoformat(raw.strip())
    except ValueError:
        _log.debug("Could not parse video creation_time %r", raw, exc_info=True)
        return None


def parse_iso6709(raw: str | None) -> tuple[float, float] | None:
    """Parse an ISO-6709 location string (e.g. '+37.7858-122.4064+010.000/') to (lat, lon).

    iPhone MOVs store GPS under ``com.apple.quicktime.location.ISO6709`` as
    consecutive signed decimal fields; only the first two (latitude, longitude)
    are used, altitude and any trailing CRS reference are ignored.
    """
    if not raw:
        return None
    nums = re.findall(r"[+-]\d+(?:\.\d+)?", raw)
    if len(nums) < 2:
        return None
    try:
        return (float(nums[0]), float(nums[1]))
    except ValueError:
        _log.debug("Could not parse ISO-6709 location %r", raw, exc_info=True)
        return None


def _parse_utc_offset(raw: str | None) -> timezone | None:
    """Parse a fixed UTC offset string (e.g. '+0100', '-08:00', 'Z') to a timezone.

    Android vendors record the capture offset in a container tag
    (``com.samsung.android.utc_offset`` = ``+0100``).
    Returns None when the value is absent or unparseable.
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw in ("Z", "+0000", "+00:00", "-0000", "-00:00"):
        return UTC
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", raw)
    if m is None:
        return None
    sign = 1 if m.group(1) == "+" else -1
    minutes = sign * (int(m.group(2)) * 60 + int(m.group(3)))
    if abs(minutes) > 14 * 60:  # outside the valid UTC-offset range
        return None
    return timezone(timedelta(minutes=minutes))


def _timezone_for(gps: tuple[float, float]) -> tzinfo | None:
    """Resolve an IANA timezone from (lat, lon) coordinates, or None.

    Uses ``tzfpy`` for the offline coordinate→zone lookup; returns None when the
    dependency is unavailable or the point falls outside any zone (e.g. open
    ocean), so the caller can fall back to the offset-unknown case.
    """
    try:
        from tzfpy import get_tz
    except ImportError:
        _log.debug("tzfpy not installed; cannot derive video timezone from GPS")
        return None
    lat, lon = gps
    tz_name = get_tz(lon, lat)  # tzfpy takes (longitude, latitude)
    if not tz_name:
        return None
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(tz_name)
    except Exception:  # pragma: no cover - malformed/unknown zone name
        _log.debug("Unknown timezone %r from GPS %r", tz_name, gps, exc_info=True)
        return None


# A filename-derived offset must land within this many minutes of a whole
# quarter-hour to be trusted (real zone offsets are all multiples of 15 min; the
# few-second slack between "recording started" in the name and the container's
# finalize time stays well under this).
_FILENAME_OFFSET_TOLERANCE_MIN = 5


def _offset_from_filename(filename: str | None, utc_dt: datetime) -> timezone | None:
    """Derive a fixed UTC offset from a timestamped filename against the UTC instant.

    Many camera apps name clips with the **local** wall-clock
    (``YYYYMMDD_HHMMSS``) while the container's ``creation_time`` is UTC. Their
    difference is the capture offset. Only trusted when it rounds to a whole
    quarter-hour within :data:`_FILENAME_OFFSET_TOLERANCE_MIN` and stays inside
    the valid ±14h UTC-offset range; otherwise (a renamed file, a name that isn't
    local time) returns None so the caller falls through to the UTC fallback.
    """
    local = datetime_from_filename(filename)
    if local is None:
        return None
    diff_min = (local - utc_dt.astimezone(UTC).replace(tzinfo=None)).total_seconds() / 60
    rounded = round(diff_min / 15) * 15
    if abs(diff_min - rounded) > _FILENAME_OFFSET_TOLERANCE_MIN or abs(rounded) > 14 * 60:
        return None
    return timezone(timedelta(minutes=rounded))


def resolve_video_time(
    metadata: dict[str, str],
    gps: tuple[float, float] | None,
    filename: str | None = None,
) -> datetime | None:
    """Resolve a video's ``date_taken`` as local wall-clock.

    Returns a timezone-aware datetime whenever a UTC ``creation_time`` is present
    (its timezone is known even when the local offset is not), and a naive
    datetime when only a filename timestamp is available. Downstream indexing
    (OEC-18) derives the naive-local ``date_taken``, the UTC instant, and the
    offset from it. Returns None only when no timestamp exists at all.

    Precedence:
      1. Apple ``com.apple.quicktime.creationdate`` — already local + offset.
      2. UTC ``creation_time`` + explicit vendor offset tag (e.g. Samsung/Android).
      3. UTC ``creation_time`` + GPS location — offset derived from coordinates.
      4. UTC ``creation_time`` + timestamped filename — offset derived from the
         local wall-clock in the name vs the UTC instant (see
         :func:`_offset_from_filename`).
      5. UTC ``creation_time`` alone — kept as tz-aware UTC (local offset unknown,
         so the derived naive-local date_taken may be off by the true offset).
      6. No ``creation_time`` at all (e.g. re-encodes that stripped it) but a
         timestamped filename — naive local wall-clock, offset unknown. Mirrors
         the photo case with no EXIF offset. Falls back to a date-only filename
         (midnight local) when the name carries a date but no time.
    """
    # 1. Apple's tag carries local wall-clock with an explicit offset.
    apple = _parse_creation_time(container_tag(metadata, "com.apple.quicktime.creationdate"))
    if apple is not None and apple.utcoffset() is not None:
        return apple

    creation = _parse_creation_time(container_tag(metadata, "creation_time"))
    if creation is None:
        # 6. No UTC anchor: fall back to a naive Apple date, else the filename's
        # local wall-clock, else a date-only filename (midnight), else nothing.
        if apple is not None:
            return apple
        return datetime_from_filename(filename) or date_from_filename(filename)

    # creation_time is UTC by spec; make that explicit before converting.
    utc_dt = creation if creation.tzinfo is not None else creation.replace(tzinfo=UTC)

    # 2. Android vendors record the capture offset in a container tag even when
    # no Apple creationdate or GPS location is present.
    offset = _parse_utc_offset(
        container_tag(metadata, "com.samsung.android.utc_offset", "com.android.utc_offset")
    )
    if offset is not None:
        return utc_dt.astimezone(offset)

    # 3. Derive the offset from the capture location.
    if gps is not None:
        zone = _timezone_for(gps)
        if zone is not None:
            return utc_dt.astimezone(zone)

    # 4. No tag or location, but many camera apps name clips with the local
    # wall-clock while creation_time is UTC — recover the offset from that gap.
    fname_offset = _offset_from_filename(filename, utc_dt)
    if fname_offset is not None:
        return utc_dt.astimezone(fname_offset)

    # 5. Local offset unknown, but creation_time is UTC by spec — that timezone is
    # itself known, so keep it (tz-aware UTC) rather than discarding it. Downstream
    # (OEC-18) records the exact UTC instant in date_taken_utc; the naive-local
    # date_taken it derives is the UTC wall-clock, which can be off by the true
    # local offset. Unlike a photo's EXIF DateTimeOriginal (genuinely naive local,
    # no tz), a video always carries at least the UTC anchor.
    return utc_dt
