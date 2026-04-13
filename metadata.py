"""Google Takeout JSON parsing and exiftool EXIF application.

Google Photos Takeout places a companion JSON file alongside each media file
containing the original capture timestamp and GPS coordinates. This module
finds those JSONs, parses the relevant fields, and batch-applies them to the
media files using a single exiftool invocation for efficiency.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

MEDIA_EXTENSIONS = frozenset({
    # Raster images
    "jpg", "jpeg", "png", "gif", "heic", "heif", "webp",
    "tif", "tiff", "bmp",
    # RAW formats
    "cr2", "cr3", "nef", "arw", "dng", "orf", "rw2", "raf",
    # Videos
    "mp4", "mov", "avi", "mkv", "webm", "3gp", "m4v", "mts", "m2ts",
})

# Video extensions that need QuickTime-specific timestamp handling
_QUICKTIME_EXTENSIONS = frozenset({"mp4", "mov", "m4v", "3gp"})


def find_json_for_media(media_path: Path) -> Path | None:
    """Locate the Takeout companion JSON file for a media file.

    Handles these naming patterns:
      - Standard:   photo.jpg        → photo.jpg.json
      - Duplicates: photo(1).jpg     → photo.jpg(1).json
      - Truncated:  very_long....jpg → very_lon....jpg.json (Google truncates)
    """
    # 1. Exact match (the common case)
    exact = media_path.parent / (media_path.name + ".json")
    if exact.exists():
        return exact

    # 2. Numbered duplicates: photo(1).jpg → photo.jpg(1).json
    m = re.match(r"^(.*?)(\(\d+\))$", media_path.stem)
    if m:
        base, num = m.group(1), m.group(2)
        candidate = media_path.parent / (base + media_path.suffix + num + ".json")
        if candidate.exists():
            return candidate

    # 3. Truncated filename: try progressively shorter prefixes of the full name
    full_name = media_path.name
    for length in range(len(full_name) - 1, max(len(full_name) - 10, 20), -1):
        candidate = media_path.parent / (full_name[:length] + ".json")
        if candidate.exists():
            return candidate

    # 4. Glob fallback for unusual truncation patterns
    stem_prefix = _glob_escape(media_path.stem[:15])
    candidates = [
        p for p in media_path.parent.glob(f"{stem_prefix}*.json")
        # Exclude double-suffixed false matches like "photo.jpg.jpg.json"
        if not p.name.endswith(".json.json")
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        ideal = media_path.name + ".json"
        return max(candidates, key=lambda p: len(os.path.commonprefix([p.name, ideal])))

    return None


def _glob_escape(s: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", s)


def parse_takeout_json(json_path: Path) -> dict:
    """Parse a Takeout metadata JSON file.

    Returns a dict with:
      taken_time  datetime | None   — capture timestamp (UTC)
      lat         float | None      — latitude
      lng         float | None      — longitude
      alt         float | None      — altitude in metres
    """
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not parse metadata JSON %s: %s", json_path, e)
        return {"taken_time": None, "lat": None, "lng": None, "alt": None}

    # Timestamp — prefer photoTakenTime, fall back to creationTime
    taken_time = None
    for ts_key in ("photoTakenTime", "creationTime"):
        ts_data = data.get(ts_key, {})
        raw_ts = ts_data.get("timestamp")
        if raw_ts:
            try:
                taken_time = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc)
                break
            except (ValueError, OSError):
                pass

    # GPS — prefer geoDataExif, fall back to geoData
    # Google uses 0.0 / 0.0 as a null sentinel ("Null Island"), so skip it.
    lat = lng = alt = None
    for geo_key in ("geoDataExif", "geoData"):
        geo = data.get(geo_key, {})
        raw_lat = float(geo.get("latitude", 0.0))
        raw_lng = float(geo.get("longitude", 0.0))
        if raw_lat != 0.0 or raw_lng != 0.0:
            lat = raw_lat
            lng = raw_lng
            raw_alt = geo.get("altitude")
            alt = float(raw_alt) if raw_alt is not None else None
            break

    return {"taken_time": taken_time, "lat": lat, "lng": lng, "alt": alt}


def build_exiftool_args(media_path: Path, metadata: dict) -> list[str]:
    """Build exiftool argument flags for the given metadata.

    Returns an empty list if there is nothing to write.
    """
    args: list[str] = ["-overwrite_original", "-m"]

    taken_time: datetime | None = metadata.get("taken_time")
    if taken_time is not None:
        ts = taken_time.strftime("%Y:%m:%d %H:%M:%S")
        args += [f"-DateTimeOriginal={ts}", f"-CreateDate={ts}"]
        # QuickTime/MP4 containers use different timestamp atoms
        if media_path.suffix.lower().lstrip(".") in _QUICKTIME_EXTENSIONS:
            args += [
                f"-TrackCreateDate={ts}",
                f"-MediaCreateDate={ts}",
                "-api", "QuickTimeUTC",
            ]

    lat = metadata.get("lat")
    lng = metadata.get("lng")
    if lat is not None and lng is not None:
        args += [
            f"-GPSLatitude={abs(lat)}",
            f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
            f"-GPSLongitude={abs(lng)}",
            f"-GPSLongitudeRef={'E' if lng >= 0 else 'W'}",
        ]
        alt = metadata.get("alt")
        if alt is not None:
            args += [
                f"-GPSAltitude={abs(alt)}",
                f"-GPSAltitudeRef={'0' if alt >= 0 else '1'}",
            ]

    # Nothing useful to write
    if args == ["-overwrite_original", "-m"]:
        return []

    return args


def fix_metadata_batch(directory: Path) -> tuple[int, int, int]:
    """Apply Takeout JSON metadata to all media files under *directory*.

    Uses a single exiftool process with an argfile (one per batch), which is
    far more efficient than spawning one process per file.

    Returns (fixed, skipped, errors).
    """
    jobs: list[tuple[Path, list[str]]] = []
    skipped = 0

    for media_path in sorted(directory.rglob("*")):
        if not media_path.is_file():
            continue
        ext = media_path.suffix.lower().lstrip(".")
        if ext not in MEDIA_EXTENSIONS:
            continue

        json_path = find_json_for_media(media_path)
        if json_path is None:
            log.debug("No companion JSON for %s", media_path.name)
            skipped += 1
            continue

        metadata = parse_takeout_json(json_path)
        args = build_exiftool_args(media_path, metadata)
        if not args:
            skipped += 1
            continue

        jobs.append((media_path, args))

    if not jobs:
        log.info("No metadata to apply (%d file(s) had no usable JSON)", skipped)
        return 0, skipped, 0

    # Write an exiftool argfile.  Each "job" block ends with -execute so that
    # exiftool processes all jobs in a single invocation.
    fd, argfile = tempfile.mkstemp(suffix=".args", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            for media_path, args in jobs:
                for arg in args:
                    f.write(arg + "\n")
                f.write(str(media_path) + "\n")
                f.write("-execute\n")

        result = subprocess.run(
            ["exiftool", "-q", "-@", argfile],
            capture_output=True,
            text=True,
        )
        if result.returncode > 1:
            log.warning("exiftool reported errors:\n%s", result.stderr.strip())

        log.info(
            "Metadata: %d file(s) updated, %d skipped (no JSON / no data)",
            len(jobs), skipped,
        )
        return len(jobs), skipped, 0
    finally:
        try:
            os.unlink(argfile)
        except OSError:
            pass
