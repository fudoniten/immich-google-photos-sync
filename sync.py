#!/usr/bin/env python3
"""Google Photos Takeout → Immich import pipeline.

Downloads Takeout archives from Google Drive (via rclone), extracts them,
applies EXIF metadata from the companion JSON files (via exiftool), and
uploads photos/videos to Immich (via immich-cli).

The pipeline is double-buffered: while one archive is uploading to Immich,
the next archive is being downloaded and prepared concurrently.

Usage:
    sync.py [options] <rclone-remote-path>

Example:
    sync.py "gdrive:Takeout"
    sync.py --dry-run "gdrive:Google Photos Takeout"
    sync.py --retry-errors --work-dir /mnt/storage/work "gdrive:Takeout"
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import metadata as md
import state as st

# ---------------------------------------------------------------------------
# Timeouts (seconds)
# ---------------------------------------------------------------------------
DOWNLOAD_TIMEOUT = 7200   # 2 hours
EXTRACT_TIMEOUT = 3600    # 1 hour
METADATA_TIMEOUT = 1800   # 30 minutes
UPLOAD_TIMEOUT = 7200     # 2 hours

log = logging.getLogger("immich-sync")


# ---------------------------------------------------------------------------
# CLI and setup
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Import Google Photos Takeout archives into Immich.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "Prerequisites: rclone must be configured (run 'rclone config'), "
            "and immich-cli must be authenticated (run 'immich login <url> <api-key>')."
        ),
    )
    p.add_argument(
        "remote_path",
        help='rclone remote path containing Takeout zips, e.g. "gdrive:Takeout"',
    )
    p.add_argument(
        "--state-file",
        default="./sync-state.json",
        metavar="PATH",
        help="Path to the JSON progress-tracking file",
    )
    p.add_argument(
        "--work-dir",
        default="./work",
        metavar="PATH",
        help="Working directory for downloads and extraction",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List files and show what would be done, then exit",
    )
    p.add_argument(
        "--retry-errors",
        action="store_true",
        help="Reset files in error state back to pending so they are retried",
    )
    p.add_argument(
        "--max-retries",
        type=int,
        default=3,
        metavar="N",
        help="Max retry attempts per file before giving up",
    )
    p.add_argument(
        "--upload-concurrency",
        type=int,
        default=4,
        metavar="N",
        help="Parallel upload threads passed to immich-cli",
    )
    return p.parse_args()


def setup_logging(work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt=datefmt)
    fh = logging.FileHandler(work_dir / "sync.log")
    fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    logging.getLogger().addHandler(fh)


def check_prerequisites() -> None:
    """Verify that all required tools are installed and configured."""
    missing = [t for t in ("rclone", "exiftool", "unzip", "immich") if not shutil.which(t)]
    if missing:
        log.error("Missing required tools: %s", ", ".join(missing))
        log.error("Ensure they are on PATH (they are declared as dependencies in flake.nix).")
        sys.exit(1)

    # Verify rclone has at least one remote configured
    result = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True)
    if not result.stdout.strip():
        log.error("No rclone remotes configured.")
        log.error("Run: rclone config")
        log.error("Choose 'Google Drive' and follow the OAuth browser flow.")
        sys.exit(1)

    # Verify immich-cli can reach the server and is authenticated
    try:
        result = subprocess.run(
            ["immich", "server-info"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        log.error("immich server-info timed out. Is the server reachable?")
        sys.exit(1)
    if result.returncode != 0:
        log.error("immich-cli is not authenticated or cannot reach the server.")
        log.error("Run: immich login <server-url> <api-key>")
        log.error("stderr: %s", result.stderr.strip())
        sys.exit(1)


def ensure_work_dirs(work_dir: Path) -> None:
    for subdir in ("download", "extract", "upload"):
        (work_dir / subdir).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover_files(remote_path: str) -> list[dict]:
    """List Takeout archive files at the given rclone path."""
    log.info("Listing files at %s ...", remote_path)
    result = run_cmd(
        ["rclone", "lsjson", "--no-modtime", remote_path],
        description=f"list {remote_path}",
        timeout=120,
    )
    entries: list[dict] = json.loads(result.stdout)
    archives = [
        e for e in entries
        if not e.get("IsDir", False)
        and Path(e["Name"]).suffix.lower() in (".zip", ".tgz")
        or e["Name"].endswith(".tar.gz")
    ]
    log.info("Found %d archive(s)", len(archives))
    return [{"name": e["Name"], "Size": e.get("Size")} for e in archives]


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

def download_file(
    filename: str,
    remote_path: str,
    work_dir: Path,
    state: dict,
    state_path: str,
) -> None:
    size = state["files"][filename].get("size_bytes")
    log.info("Downloading %s%s ...", filename, f" ({_fmt_size(size)})" if size else "")
    st.update_file_status(state, state_path, filename, st.FileStatus.DOWNLOADING)
    remote_base = remote_path.rstrip("/")
    run_cmd(
        ["rclone", "copy", "--progress", f"{remote_base}/{filename}", str(work_dir / "download")],
        description=f"download {filename}",
        timeout=DOWNLOAD_TIMEOUT,
    )
    st.update_file_status(state, state_path, filename, st.FileStatus.DOWNLOADED)
    log.info("Download complete: %s", filename)


def extract_file(
    filename: str,
    work_dir: Path,
    state: dict,
    state_path: str,
) -> Path:
    archive = work_dir / "download" / filename
    stem = _archive_stem(filename)
    dest = work_dir / "extract" / stem
    dest.mkdir(parents=True, exist_ok=True)

    log.info("Extracting %s ...", filename)
    st.update_file_status(state, state_path, filename, st.FileStatus.EXTRACTING)

    if filename.endswith(".zip"):
        run_cmd(
            ["unzip", "-q", "-o", str(archive), "-d", str(dest)],
            description=f"extract {filename}",
            timeout=EXTRACT_TIMEOUT,
        )
    elif filename.endswith((".tgz", ".tar.gz")):
        run_cmd(
            ["tar", "xzf", str(archive), "-C", str(dest)],
            description=f"extract {filename}",
            timeout=EXTRACT_TIMEOUT,
        )
    else:
        raise ValueError(f"Unrecognised archive format: {filename}")

    archive.unlink()  # free disk space immediately
    st.update_file_status(state, state_path, filename, st.FileStatus.EXTRACTED)
    log.info("Extracted: %s → %s", filename, dest)
    return dest


def fix_file_metadata(
    filename: str,
    extract_dir: Path,
    state: dict,
    state_path: str,
) -> None:
    log.info("Applying metadata for %s ...", filename)
    st.update_file_status(state, state_path, filename, st.FileStatus.METADATA_FIXING)
    target = _find_google_photos_dir(extract_dir)
    fixed, skipped, errors = md.fix_metadata_batch(target)
    log.info("Metadata: %d fixed, %d skipped, %d errors", fixed, skipped, errors)
    st.update_file_status(state, state_path, filename, st.FileStatus.METADATA_FIXED)


def move_to_upload(filename: str, work_dir: Path) -> Path:
    """Move the extracted directory into the upload staging area."""
    stem = _archive_stem(filename)
    src = work_dir / "extract" / stem
    dest = work_dir / "upload" / stem
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(src), str(dest))
    return dest


def upload_to_immich(
    filename: str,
    upload_dir: Path,
    concurrency: int,
    state: dict,
    state_path: str,
) -> None:
    """Upload to Immich via immich-cli. Runs in a background thread."""
    log.info("Uploading %s to Immich ...", filename)
    st.update_file_status(state, state_path, filename, st.FileStatus.UPLOADING)
    target = _find_google_photos_dir(upload_dir)
    run_cmd(
        [
            "immich", "upload",
            "--recursive",
            "--album",
            "--concurrency", str(concurrency),
            str(target),
        ],
        description=f"upload {filename}",
        timeout=UPLOAD_TIMEOUT,
    )
    st.update_file_status(state, state_path, filename, st.FileStatus.COMPLETED)
    log.info("Upload complete: %s", filename)


def cleanup_batch(filename: str, work_dir: Path) -> None:
    """Remove the uploaded directory to free disk space."""
    stem = _archive_stem(filename)
    upload_dir = work_dir / "upload" / stem
    if upload_dir.exists():
        shutil.rmtree(upload_dir)
        log.info("Cleaned up: %s", upload_dir.name)


# ---------------------------------------------------------------------------
# Double-buffered pipeline
# ---------------------------------------------------------------------------

def run_pipeline(args: argparse.Namespace, state: dict, state_path: str) -> None:
    """Process all pending archives with download/extract/upload overlap.

    While one archive is uploading to Immich in a background thread, the next
    archive is being downloaded, extracted, and metadata-fixed on the main
    thread — so network and disk I/O are kept busy simultaneously.
    """
    work_dir = Path(args.work_dir)
    upload_future: Future | None = None
    uploading_file: str | None = None

    with ThreadPoolExecutor(max_workers=2) as executor:
        while True:
            next_file = st.get_next_pending(state)

            if next_file is None and upload_future is None:
                break  # everything is done

            if next_file is not None:
                try:
                    download_file(next_file, args.remote_path, work_dir, state, state_path)
                    extract_dir = extract_file(next_file, work_dir, state, state_path)
                    fix_file_metadata(next_file, extract_dir, state, state_path)
                    upload_dir = move_to_upload(next_file, work_dir)

                    # Wait for the previous upload to finish before starting the next
                    if upload_future is not None:
                        _await_upload(upload_future, uploading_file, work_dir, state, state_path)
                        upload_future = None

                    uploading_file = next_file
                    upload_future = executor.submit(
                        upload_to_immich,
                        next_file,
                        upload_dir,
                        args.upload_concurrency,
                        state,
                        state_path,
                    )

                except Exception as e:
                    log.error("Failed processing %s: %s", next_file, e)
                    st.update_file_status(
                        state, state_path, next_file, st.FileStatus.ERROR, str(e)
                    )

            else:
                # No more archives to download; wait for the final upload
                _await_upload(upload_future, uploading_file, work_dir, state, state_path)
                upload_future = None
                uploading_file = None


def _await_upload(
    future: Future | None,
    filename: str | None,
    work_dir: Path,
    state: dict,
    state_path: str,
) -> None:
    if future is None:
        return
    try:
        future.result()
        if filename:
            cleanup_batch(filename, work_dir)
    except Exception as e:
        log.error("Upload failed for %s: %s", filename, e)
        if filename:
            st.update_file_status(state, state_path, filename, st.FileStatus.ERROR, str(e))


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def run_cmd(
    cmd: list[str],
    description: str = "",
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    log.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({description}): exit {result.returncode}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result


def _archive_stem(filename: str) -> str:
    """Strip archive extension(s) to get a clean directory name."""
    p = Path(filename)
    if p.suffix == ".gz" and p.stem.endswith(".tar"):
        return Path(p.stem).stem
    return p.stem


def _find_google_photos_dir(base: Path) -> Path:
    """Locate the 'Google Photos' directory inside an extracted Takeout archive.

    If not found (unexpected archive structure), falls back to *base* so the
    upload still proceeds.
    """
    for candidate in base.rglob("Google Photos"):
        if candidate.is_dir():
            return candidate
    log.warning(
        "Could not find 'Google Photos' directory in %s; uploading from root", base
    )
    return base


def _fmt_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "unknown size"
    n = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def print_summary(state: dict) -> None:
    counts = st.summary(state)
    total = sum(counts.values())
    log.info("=" * 50)
    log.info("Summary (%d total archives):", total)
    for status, count in sorted(counts.items()):
        log.info("  %-20s %d", status, count)
    log.info("=" * 50)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    work_dir = Path(args.work_dir)
    setup_logging(work_dir)
    log.info("immich-google-photos-sync starting")

    check_prerequisites()
    ensure_work_dirs(work_dir)

    state_path = str(Path(args.state_file).resolve())
    state = st.load_state(state_path)
    state["remote"] = args.remote_path

    reset_count = st.reset_interrupted(state)
    if reset_count:
        log.info("Reset %d interrupted archive(s) to safe state for retry", reset_count)

    if args.retry_errors:
        for info in state["files"].values():
            if (
                info.get("status") == st.FileStatus.ERROR.value
                and info.get("retries", 0) < args.max_retries
            ):
                info["status"] = st.FileStatus.PENDING.value
        log.info("Reset eligible errored archives back to pending")

    discovered = discover_files(args.remote_path)
    new_count = st.add_discovered_files(state, discovered)
    st.save_state(state_path, state)

    pending = st.get_files_by_status(state, st.FileStatus.PENDING)
    completed = st.get_files_by_status(state, st.FileStatus.COMPLETED)
    log.info(
        "%d pending, %d already completed, %d total",
        len(pending), len(completed), len(state["files"]),
    )
    if new_count:
        log.info("Newly discovered: %d archive(s)", new_count)

    if args.dry_run:
        log.info("Dry run — would process:")
        for f in pending:
            size = state["files"][f].get("size_bytes")
            log.info("  %s  %s", f, _fmt_size(size))
        return

    if not pending:
        log.info("Nothing to do — all archives completed.")
        print_summary(state)
        return

    run_pipeline(args, state, state_path)
    print_summary(state)


if __name__ == "__main__":
    main()
