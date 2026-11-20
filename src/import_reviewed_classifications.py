#!/usr/bin/env python3
"""Append confirmed rows from a local-finance-tracker classification review CSV to training data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sqlite3
import sys
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "local-finance-tracker.sqlite"
REQUIRED_COLUMNS = {"Source row", "Date", "Type", "Description", "Value", "Account Name", "Predicted Category", "Review Status"}


def normalise(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def pounds_to_pence(value: str, line: int) -> int:
    try:
        amount = Decimal(value.strip().replace("£", "").replace(",", ""))
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"CSV line {line}: invalid Value") from exc
    if not amount.is_finite() or amount.as_tuple().exponent < -2:
        raise ValueError(f"CSV line {line}: Value must be a finite amount with at most 2 decimal places")
    return int(amount * 100)


def parse_date(value: str, line: int) -> str:
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"CSV line {line}: invalid Date")


def read_rows(path: Path, categories: set[str]) -> list[dict[str, object]]:
    accepted: list[dict[str, object]] = []
    seen_rows: set[int] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("Review CSV has no header")
        missing = REQUIRED_COLUMNS - {field.strip() for field in reader.fieldnames}
        if missing:
            raise ValueError("Missing columns: " + ", ".join(sorted(missing)))
        for line, source_row in enumerate(reader, start=2):
            row = {(key or "").strip(): value or "" for key, value in source_row.items()}
            if row.get("Review Status", "").strip().casefold() != "reviewed":
                continue
            try:
                source_number = int(row["Source row"].strip())
            except ValueError as exc:
                raise ValueError(f"CSV line {line}: invalid Source row") from exc
            if source_number < 2 or source_number in seen_rows:
                raise ValueError(f"CSV line {line}: Source row must be unique and at least 2")
            seen_rows.add(source_number)
            category = row["Predicted Category"].strip()
            if category not in categories:
                raise ValueError(f"CSV line {line}: unknown category {category!r}")
            description = row["Description"].strip()
            account_name = row["Account Name"].strip()
            transaction_type = row["Type"].strip()
            if not all((description, account_name, transaction_type)):
                raise ValueError(f"CSV line {line}: Type, Description and Account Name must not be blank")
            accepted.append({
                "source_row": source_number,
                "date": parse_date(row["Date"], line),
                "type": transaction_type,
                "description": description,
                "description_key": normalise(description),
                "amount": pounds_to_pence(row["Value"], line),
                "account": account_name,
                "account_key": normalise(account_name),
                "category": category,
            })
    if not accepted:
        raise ValueError("No rows marked REVIEWED were found")
    return accepted


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review_csv", type=Path)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--predictions", type=Path, help="original unreviewed suggestion CSV, used to record the model's prior label")
    args = parser.parse_args()
    review_csv = args.review_csv.expanduser().resolve()
    database = args.database.expanduser().resolve()
    predictions = args.predictions.expanduser().resolve() if args.predictions else None
    if not review_csv.is_file() or not database.is_file():
        parser.error("Review CSV and database must both exist")
    digest = sha256(review_csv)
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            categories = {row[0] for row in connection.execute("SELECT name FROM categories")}
            already = connection.execute("SELECT training_import_id FROM training_imports WHERE file_sha256=?", (digest,)).fetchone()
        if already:
            raise ValueError(f"This review file was already imported as training import {already[0]}")
        rows = read_rows(review_csv, categories)

        original_labels: dict[int, str] = {}
        if predictions:
            with predictions.open("r", encoding="utf-8-sig", newline="") as source:
                reader = csv.DictReader(source)
                for row in reader:
                    try:
                        original_labels[int(row["Source row"])] = row["Predicted Category"].strip()
                    except (KeyError, ValueError):
                        continue

        # A database backup makes this append operation straightforward to roll back.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = database.with_name(f"{database.stem}.before-reviewed-import-{stamp}{database.suffix}")
        with sqlite3.connect(database) as source_db, sqlite3.connect(backup) as backup_db:
            source_db.backup(backup_db)
        if os.name == "posix":
            try:
                os.chmod(backup, 0o600)
            except OSError:
                pass

        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            category_ids = {name: category_id for category_id, name in connection.execute("SELECT category_id, name FROM categories")}
            cursor = connection.execute(
                "INSERT INTO training_imports(filename,file_sha256,row_count) VALUES (?,?,?)",
                (review_csv.name, digest, len(rows)),
            )
            training_import_id = cursor.lastrowid
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for row in rows:
                old_label = original_labels.get(int(row["source_row"]))
                original_category_id = category_ids.get(old_label) if old_label != row["category"] else None
                connection.execute(
                    """INSERT INTO training_examples (
                           training_import_id, source_row_number, transaction_date,
                           description, description_key, amount_pence, account_name,
                           account_name_key, source_category, category_id,
                           label_origin, original_category_id, reviewed_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?, 'manual', ?, ?)""",
                    (training_import_id, row["source_row"], row["date"], row["description"],
                     row["description_key"], row["amount"], row["account"], row["account_key"],
                     row["category"], category_ids[str(row["category"])], original_category_id, now),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        changed = sum(original_labels.get(int(row["source_row"])) not in (None, row["category"]) for row in rows)
        print(f"Imported {len(rows)} reviewed transactions as training examples (ID {training_import_id}).")
        print(f"Labels changed from the original model suggestions: {changed}")
        print("Unreviewed and removed rows were not imported; no bank transaction or spending-report rows were created.")
        print(f"Database backup: {backup}")
        return 0
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
