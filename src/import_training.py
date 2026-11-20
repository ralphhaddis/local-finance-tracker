#!/usr/bin/env python3
"""Preview and import labelled historical examples, separate from spending transactions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "local-finance-tracker.sqlite"
REQUIRED_COLUMNS = {"Description", "Category", "Date", "Value", "Account Name"}
LABEL_MAP = {"Utilites": "Utilities", "Eating out": "Going out"}


@dataclass(frozen=True)
class Example:
    source_row_number: int
    transaction_date: str | None
    description: str
    description_key: str
    amount_pence: int | None
    account_name: str | None
    account_name_key: str | None
    source_category: str
    category_name: str

    @property
    def exact_key(self) -> tuple[str, int, str, str] | None:
        if self.transaction_date is None or self.amount_pence is None or self.account_name_key is None:
            return None
        return (self.transaction_date, self.amount_pence, self.account_name_key, self.description_key)


def normalise(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def to_pence(value: str, row_number: int) -> int:
    try:
        amount = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"Row {row_number}: invalid amount {value!r}") from exc
    if not amount.is_finite() or amount.as_tuple().exponent < -2:
        raise ValueError(f"Row {row_number}: amount must be finite with at most 2 decimal places")
    return int(amount * 100)


def parse_examples(path: Path) -> tuple[list[Example], str]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    examples: list[Example] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                raise ValueError("The file is empty or has no CSV header")
            headers = {header.strip() for header in reader.fieldnames}
            missing = REQUIRED_COLUMNS - headers
            if missing:
                raise ValueError("Missing required columns: " + ", ".join(sorted(missing)))

            for row_number, row in enumerate(reader, start=2):
                if None in row:
                    raise ValueError(f"Row {row_number}: more values than the CSV header")
                if not any((value or "").strip() for value in row.values()):
                    continue
                clean = {(key or "").strip(): (value or "").strip() for key, value in row.items()}
                description = clean.get("Description", "")
                source_category = clean.get("Category", "")
                if not description or not source_category:
                    raise ValueError(f"Row {row_number}: Description and Category are required")

                date_text = clean.get("Date", "")
                date_iso: str | None = None
                if date_text:
                    try:
                        date_iso = datetime.strptime(date_text, "%d %b %Y").date().isoformat()
                    except ValueError as exc:
                        raise ValueError(f"Row {row_number}: invalid date {date_text!r}; expected e.g. '21 Sep 2026'") from exc

                value_text = clean.get("Value", "")
                amount = to_pence(value_text, row_number) if value_text else None
                account = clean.get("Account Name", "") or None
                category_name = LABEL_MAP.get(source_category, source_category)
                examples.append(
                    Example(
                        source_row_number=row_number,
                        transaction_date=date_iso,
                        description=description,
                        description_key=normalise(description),
                        amount_pence=amount,
                        account_name=account,
                        account_name_key=normalise(account) if account else None,
                        source_category=source_category,
                        category_name=category_name,
                    )
                )
    except UnicodeDecodeError as exc:
        raise ValueError("Could not read this file as UTF-8 CSV") from exc
    if not examples:
        raise ValueError("No labelled examples found")
    return examples, digest


def exact_key_stats(examples: list[Example]) -> tuple[int, int]:
    labels: dict[tuple[str, int, str, str], set[str]] = defaultdict(set)
    for example in examples:
        if example.exact_key is not None:
            labels[example.exact_key].add(example.category_name)
    exact_eligible = sum(example.exact_key is not None for example in examples)
    ambiguous = sum(len(categories) > 1 for categories in labels.values())
    return exact_eligible, ambiguous


def preview(path: Path, database: Path, examples: list[Example], digest: str) -> bool:
    counts = Counter(example.source_category for example in examples)
    eligible, ambiguous = exact_key_stats(examples)
    print(f"File: {path.name}")
    print(f"Labelled examples: {len(examples)}")
    print(f"Complete transaction keys available for exact lookup: {eligible}")
    print(f"Exact keys with conflicting labels (will not auto-match): {ambiguous}")
    print("Categories: " + ", ".join(f"{name}={count}" for name, count in sorted(counts.items())))
    print("Category normalisation: Utilites -> Utilities; Eating out -> Going out")
    print(f"SHA-256: {digest}")
    if database.exists():
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT training_import_id FROM training_imports WHERE file_sha256 = ?", (digest,)
            ).fetchone()
        if row:
            print(f"\nThis exact training file was already imported (training import ID {row[0]}). No changes made.")
            return True
    return False


def import_examples(path: Path, database: Path, examples: list[Example], digest: str) -> int:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        already = connection.execute(
            "SELECT training_import_id FROM training_imports WHERE file_sha256 = ?", (digest,)
        ).fetchone()
        if already:
            raise ValueError(f"This exact file was already imported as training import ID {already[0]}")

        cursor = connection.execute(
            "INSERT INTO training_imports (filename, file_sha256, row_count) VALUES (?, ?, ?)",
            (path.name, digest, len(examples)),
        )
        training_import_id = cursor.lastrowid
        category_ids: dict[str, int] = {}
        for example in examples:
            if example.category_name not in category_ids:
                if example.category_name == "Income":
                    kind = "income"
                elif example.category_name == "Transfer":
                    kind = "transfer"
                else:
                    kind = "spending"
                connection.execute(
                    "INSERT INTO categories (name, kind) VALUES (?, ?) ON CONFLICT(name) DO NOTHING",
                    (example.category_name, kind),
                )
                category_row = connection.execute(
                    "SELECT category_id, kind FROM categories WHERE name = ?", (example.category_name,)
                ).fetchone()
                if category_row[1] != kind:
                    raise ValueError(f"Category {example.category_name!r} already exists with kind {category_row[1]!r}, expected {kind!r}")
                category_ids[example.category_name] = category_row[0]

            connection.execute(
                """INSERT INTO training_examples (
                       training_import_id, source_row_number, transaction_date,
                       description, description_key, amount_pence, account_name,
                       account_name_key, source_category, category_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    training_import_id,
                    example.source_row_number,
                    example.transaction_date,
                    example.description,
                    example.description_key,
                    example.amount_pence,
                    example.account_name,
                    example.account_name_key,
                    example.source_category,
                    category_ids[example.category_name],
                ),
            )
        connection.commit()
        return training_import_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", type=Path, help="Path to labelled training CSV")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--commit", action="store_true", help="Import after preview and explicit confirmation")
    args = parser.parse_args()
    path = args.csv_file.expanduser().resolve()
    database = args.database.expanduser().resolve()
    if not path.is_file():
        print(f"Error: training CSV not found: {path}", file=sys.stderr)
        return 2
    try:
        examples, digest = parse_examples(path)
        already_imported = preview(path, database, examples, digest)
        if already_imported or not args.commit:
            if not already_imported:
                print("\nPreview only. Re-run with --commit to import these labelled examples.")
            return 0
        if input("\nType IMPORT TRAINING to save examples to SQLite: ").strip() != "IMPORT TRAINING":
            print("Import cancelled; database unchanged.")
            return 0
        import_id = import_examples(path, database, examples, digest)
        print(f"Imported {len(examples)} labelled examples (training import ID {import_id}).")
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
