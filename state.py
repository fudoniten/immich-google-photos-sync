"""State file management for the Takeout → Immich import pipeline.

The state file is a JSON file tracking each Takeout archive through the
pipeline. Writes are atomic (write-to-tmp + rename) and use fcntl locking
to prevent corruption if two processes run simultaneously.
"""

import enum
import fcntl
import json
import os
from datetime import datetime, timezone


class FileStatus(str, enum.Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    METADATA_FIXING = "metadata_fixing"
    METADATA_FIXED = "metadata_fixed"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    ERROR = "error"


# When interrupted mid-operation, roll back to the last safe state so the
# operation is retried cleanly on the next run.
INTERRUPTED_RESET: dict[FileStatus, FileStatus] = {
    FileStatus.DOWNLOADING: FileStatus.PENDING,
    FileStatus.EXTRACTING: FileStatus.DOWNLOADED,
    FileStatus.METADATA_FIXING: FileStatus.EXTRACTED,
    FileStatus.UPLOADING: FileStatus.METADATA_FIXED,
}

_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_entry(size_bytes: int | None = None) -> dict:
    return {
        "status": FileStatus.PENDING.value,
        "size_bytes": size_bytes,
        "started_at": None,
        "completed_at": None,
        "error": None,
        "retries": 0,
    }


def load_state(path: str) -> dict:
    """Load the state file, or return a fresh state if it doesn't exist."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"version": _VERSION, "remote": "", "files": {}}
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Corrupt state file {path}: {e}") from e


def save_state(path: str, state: dict) -> None:
    """Atomically write the state dict to disk with exclusive locking."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        json.dump(state, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic rename


def update_file_status(
    state: dict,
    state_path: str,
    filename: str,
    new_status: FileStatus,
    error: str | None = None,
) -> None:
    """Transition a file to a new status and persist the state file."""
    entry = state["files"].setdefault(filename, _default_entry())
    prev_status = entry.get("status")
    entry["status"] = new_status.value
    entry["error"] = error

    if new_status == FileStatus.DOWNLOADING and prev_status == FileStatus.PENDING.value:
        entry["started_at"] = _now()
    if new_status in (FileStatus.COMPLETED, FileStatus.ERROR):
        entry["completed_at"] = _now()
    if new_status == FileStatus.ERROR:
        entry["retries"] = entry.get("retries", 0) + 1

    save_state(state_path, state)


def get_files_by_status(state: dict, status: FileStatus) -> list[str]:
    return [
        name
        for name, info in state["files"].items()
        if info.get("status") == status.value
    ]


def get_next_pending(state: dict) -> str | None:
    """Return the first file in pending state, preserving insertion order."""
    for name, info in state["files"].items():
        if info.get("status") == FileStatus.PENDING.value:
            return name
    return None


def reset_interrupted(state: dict) -> int:
    """On startup, roll back any in-flight statuses to their safe predecessors.

    Returns the number of files that were reset.
    """
    count = 0
    for info in state["files"].values():
        try:
            current = FileStatus(info.get("status", "pending"))
        except ValueError:
            continue
        if current in INTERRUPTED_RESET:
            info["status"] = INTERRUPTED_RESET[current].value
            count += 1
    return count


def add_discovered_files(state: dict, files: list[dict]) -> int:
    """Merge newly discovered archive files into state as pending.

    Existing entries are left untouched. Returns count of newly added files.
    """
    added = 0
    for f in files:
        name = f["name"]
        if name not in state["files"]:
            state["files"][name] = _default_entry(size_bytes=f.get("Size"))
            added += 1
    return added


def summary(state: dict) -> dict[str, int]:
    """Return a count of files in each status."""
    counts: dict[str, int] = {}
    for info in state["files"].values():
        s = info.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    return counts
