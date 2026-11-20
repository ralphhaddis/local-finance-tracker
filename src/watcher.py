"""Stable-file polling and local NatWest import/classification workflow."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from classifier_service import (
    MODEL_NAME, TrainingDataUnavailableError, predict_many, train_from_database,
)
from import_natwest import (DEFAULT_DATABASE, commit_import, detect_internal_transfer_pairs,
                            parse_csv, remove_already_imported_rows)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INBOX = PROJECT_ROOT / "inbox"
ARCHIVED = PROJECT_ROOT / "archived"
REJECTED = PROJECT_ROOT / "rejected"
ACTIVITY = PROJECT_ROOT / "data" / "activity.jsonl"
POLL_SECONDS = 3
_scan_lock = threading.Lock()
_seen: dict[Path, tuple[int, int, int]] = {}
_retry_after: dict[Path, float] = {}
_thread: threading.Thread | None = None


def _log(event: str, filename: str, detail: str = "") -> None:
    ACTIVITY.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(ACTIVITY.parent, 0o700)
        except OSError:
            pass
    entry = {"at": datetime.now().astimezone().isoformat(timespec="seconds"),
             "event": event, "filename": filename, "detail": detail}
    with ACTIVITY.open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if os.name == "posix":
        try:
            os.chmod(ACTIVITY, 0o600)
        except OSError:
            pass


def _unique_destination(folder: Path, filename: str, suffix: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    stem, ext = Path(filename).stem, Path(filename).suffix
    destination = folder / f"{stem}{suffix}{ext}"
    if not destination.exists():
        return destination
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return folder / f"{stem}-{stamp}{suffix}{ext}"


def _existing_hash(database: Path, digest: str) -> bool:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        return connection.execute("SELECT 1 FROM imports WHERE file_sha256=?", (digest,)).fetchone() is not None


def process_file(path: Path, database: Path = DEFAULT_DATABASE) -> str:
    """Import and classify one stable CSV. Returns imported, duplicate, rejected, or retry."""
    if path.suffix.casefold() != ".csv":
        file_type = path.suffix or "(no extension)"
        reason = f"Unsupported file type {file_type}. local-finance-tracker accepts NatWest CSV exports (.csv). Export or download this statement as CSV, then drop it in inbox/."
        dest = _unique_destination(REJECTED, path.name)
        shutil.move(str(path), dest)
        dest.with_suffix(dest.suffix + ".error.txt").write_text(reason + "\n", encoding="utf-8")
        _log("rejected", path.name, reason)
        return "rejected"
    try:
        transactions, digest = parse_csv(path)
    except (OSError, ValueError) as exc:
        dest = _unique_destination(REJECTED, path.name)
        shutil.move(str(path), dest)
        reason_path = dest.with_suffix(dest.suffix + ".error.txt")
        reason_path.write_text(str(exc) + "\n", encoding="utf-8")
        _log("rejected", path.name, str(exc))
        return "rejected"

    unspecified_types = sum(t.transaction_type == "Unspecified" for t in transactions)

    try:
        if _existing_hash(database, digest):
            shutil.move(str(path), _unique_destination(ARCHIVED, path.name, ".duplicate"))
            _log("duplicate", path.name, f"{len(transactions)} rows; already imported")
            return "duplicate"
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            new_transactions, overlaps = remove_already_imported_rows(connection, transactions)
            if not new_transactions:
                commit_import(path, database, [], digest)
                shutil.move(str(path), _unique_destination(ARCHIVED, path.name, ".overlap"))
                _log("overlap", path.name, f"all {len(transactions)} rows already exist; file recorded with zero new rows")
                return "duplicate"
            transfer_rows, transfer_existing_ids = detect_internal_transfer_pairs(connection, new_transactions)
            model_transactions = [t for t in new_transactions if t.source_row_number not in transfer_rows]
            model_available = True
            if model_transactions:
                try:
                    model = train_from_database(connection)
                except TrainingDataUnavailableError as exc:
                    model = None
                    model_available = False
                    no_model_reason = str(exc)
                else:
                    category_ids = {row[1]: row[0] for row in connection.execute("SELECT category_id,name FROM categories")}
            else:
                model = None
                category_ids = {}
        suggestions: dict[int, tuple[int, float]] = {}
        if model_available and model_transactions:
            predictions = predict_many(
                model, [(transaction.description, transaction.amount_pence) for transaction in model_transactions]
            )
            for transaction, (category, confidence) in zip(model_transactions, predictions):
                suggestions[transaction.source_row_number] = (category_ids[category], confidence)
        import_id = commit_import(path, database, new_transactions, digest, suggestions,
                                  MODEL_NAME if model_available else None,
                                  transfer_rows, transfer_existing_ids)
        shutil.move(str(path), _unique_destination(ARCHIVED, path.name))
        detail = f"import_id={import_id}; imported_rows={len(new_transactions)}; overlap_rows_skipped={overlaps}; automatic transfer transactions={len(transfer_rows)}; model suggestions require review"
        if not model_available:
            detail += f"; no model suggestions yet ({no_model_reason}); transactions left pending review"
        if unspecified_types:
            detail += f"; {unspecified_types} row(s) had blank Type and were retained as Unspecified"
        _log("imported", path.name, detail)
        return "imported"
    except Exception as exc:
        # Keep operational failures in the inbox for retry; no statement contents go into the activity log.
        _retry_after[path] = time.monotonic() + 30
        _log("retry", path.name, f"{type(exc).__name__}: {exc}")
        return "retry"


def scan_once() -> dict[str, int]:
    """Process files unchanged across two consecutive watcher scans."""
    outcomes = {"imported": 0, "duplicate": 0, "rejected": 0, "retry": 0, "waiting": 0}
    INBOX.mkdir(parents=True, exist_ok=True)
    ARCHIVED.mkdir(parents=True, exist_ok=True)
    REJECTED.mkdir(parents=True, exist_ok=True)
    if not _scan_lock.acquire(blocking=False):
        return outcomes
    try:
        present: set[Path] = set()
        for path in sorted(INBOX.iterdir()):
            if path.is_symlink() or not path.is_file() or path.name.startswith(".") or path.suffix.casefold() in {".crdownload", ".part", ".download"}:
                continue
            present.add(path)
            try:
                stat = path.stat()
            except OSError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            previous = _seen.get(path)
            count = previous[2] + 1 if previous and previous[:2] == signature else 1
            _seen[path] = (signature[0], signature[1], count)
            if count < 2 or time.monotonic() < _retry_after.get(path, 0):
                outcomes["waiting"] += 1
                continue
            status = process_file(path)
            outcomes[status] += 1
            if status != "retry":
                _seen.pop(path, None)
                _retry_after.pop(path, None)
    finally:
        for path in list(_seen):
            if path not in present:
                _seen.pop(path, None)
                _retry_after.pop(path, None)
        _scan_lock.release()
    return outcomes


def _watch_loop() -> None:
    while True:
        try:
            scan_once()
        except Exception as exc:
            _log("watcher_error", "", f"{type(exc).__name__}: {exc}")
        time.sleep(POLL_SECONDS)


def start_watcher() -> threading.Thread:
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_watch_loop, name="tracker-folder-watcher", daemon=True)
        _thread.start()
    return _thread
