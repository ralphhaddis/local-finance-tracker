#!/usr/bin/env python3
"""Interactively assign categories to unclassified or review-required transactions."""

from __future__ import annotations

import hashlib
import argparse
import sqlite3
import sys
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from import_natwest import apply_merchant_training_matches


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE = PROJECT_ROOT / "data" / "local-finance-tracker.sqlite"


def normalise(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def money(pence: int) -> str:
    return f"£{pence / 100:,.2f}"


def pending_transactions(connection: sqlite3.Connection, include_all: bool = False) -> list[sqlite3.Row]:
    condition = "1 = 1" if include_all else "t.category_id IS NULL OR t.review_required = 1"
    return connection.execute(
        f"""SELECT t.transaction_id, t.transaction_date, t.raw_description,
                  t.amount_pence, t.category_id, t.category_source,
                  c.name AS current_category, a.account_name
           FROM transactions AS t
           JOIN accounts AS a ON a.account_id = t.account_id
           LEFT JOIN categories AS c ON c.category_id = t.category_id
           WHERE {condition}
           ORDER BY t.transaction_date, t.transaction_id"""
    ).fetchall()


def apply_exact_training_matches(connection: sqlite3.Connection) -> int:
    """Resolve exact historical matches before asking the user to classify anything."""
    rows = connection.execute(
        """SELECT t.transaction_id, t.transaction_date, t.raw_description,
                  t.amount_pence, a.account_name
           FROM transactions AS t
           JOIN accounts AS a ON a.account_id = t.account_id
           WHERE t.category_id IS NULL OR t.review_required = 1"""
    ).fetchall()
    resolved: list[tuple[int, int]] = []
    for transaction in rows:
        matches = connection.execute(
            """SELECT DISTINCT category_id, label_origin = 'manual' AS manually_confirmed
               FROM training_examples
               WHERE transaction_date = ? AND amount_pence = ?
                 AND account_name_key = ? AND description_key = ?""",
            (
                transaction["transaction_date"],
                transaction["amount_pence"],
                normalise(transaction["account_name"]),
                normalise(transaction["raw_description"]),
            ),
        ).fetchall()
        confirmed = [match for match in matches if match[1]]
        candidates = confirmed if confirmed else matches
        category_ids = {match[0] for match in candidates}
        if len(category_ids) == 1:
            resolved.append((next(iter(category_ids)), transaction["transaction_id"]))

    if resolved:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                """UPDATE transactions
                   SET category_id = ?, category_source = 'training_lookup', review_required = 0
                   WHERE transaction_id = ?""",
                resolved,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return len(resolved)


def add_or_update_training_example(
    connection: sqlite3.Connection,
    transaction: sqlite3.Row,
    category_id: int,
    category_name: str,
    batch_id: int,
    source_row_number: int,
) -> bool:
    """Save a confirmed label. Update it in place if this transaction was reviewed before."""
    account_key = normalise(transaction["account_name"])
    description_key = normalise(transaction["raw_description"])
    existing = connection.execute(
        "SELECT example_id FROM training_examples WHERE source_transaction_id = ?",
        (transaction["transaction_id"],),
    ).fetchone()
    if existing:
        connection.execute(
            """UPDATE training_examples
               SET transaction_date = ?, description = ?, description_key = ?,
                   amount_pence = ?, account_name = ?, account_name_key = ?,
                   source_category = ?, category_id = ?, label_origin = 'manual',
                   reviewed_at = CURRENT_TIMESTAMP
               WHERE example_id = ?""",
            (
                transaction["transaction_date"],
                transaction["raw_description"],
                description_key,
                transaction["amount_pence"],
                transaction["account_name"],
                account_key,
                category_name,
                category_id,
                existing[0],
            ),
        )
        return False

    connection.execute(
        """INSERT INTO training_examples (
               training_import_id, source_row_number, transaction_date,
               description, description_key, amount_pence, account_name,
               account_name_key, source_category, category_id, source_transaction_id,
               label_origin, reviewed_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', CURRENT_TIMESTAMP)""",
        (
            batch_id,
            source_row_number,
            transaction["transaction_date"],
            transaction["raw_description"],
            description_key,
            transaction["amount_pence"],
            transaction["account_name"],
            account_key,
            category_name,
            category_id,
            transaction["transaction_id"],
        ),
    )
    return True


def save_choice(
    connection: sqlite3.Connection,
    transaction: sqlite3.Row,
    category_id: int,
    category_name: str,
    batch_id: int,
    source_row_number: int,
) -> bool:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """UPDATE transactions
               SET category_id = ?, category_source = 'manual', review_required = 0
               WHERE transaction_id = ?""",
            (category_id, transaction["transaction_id"]),
        )
        added = add_or_update_training_example(
            connection, transaction, category_id, category_name, batch_id, source_row_number
        )
        if added:
            connection.execute(
                "UPDATE training_imports SET row_count = row_count + 1 WHERE training_import_id = ?",
                (batch_id,),
            )
        connection.commit()
        return added
    except Exception:
        connection.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="include already classified transactions so you can correct them")
    args = parser.parse_args()
    if not DATABASE.is_file():
        print(f"Database not found: {DATABASE}", file=sys.stderr)
        return 2

    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        categories = connection.execute(
            "SELECT category_id, name, kind FROM categories WHERE name <> 'Uncategorised' ORDER BY kind, name"
        ).fetchall()
        if not categories:
            print("No categories are configured.", file=sys.stderr)
            return 1

        exact_matches = apply_exact_training_matches(connection)
        merchant_matches = apply_merchant_training_matches(connection)
        auto_classified = exact_matches + merchant_matches
        if auto_classified:
            print(f"Applied {exact_matches} exact and {merchant_matches} consistent-merchant training match(es).\n")
        pending = pending_transactions(connection, include_all=args.all)
        if not pending:
            print("No transactions need category review.")
            return 0

        batch_token = uuid.uuid4().hex
        batch_hash = hashlib.sha256(("local-finance-tracker-manual-review:" + batch_token).encode()).hexdigest()
        batch_cursor = connection.execute(
            "INSERT INTO training_imports (filename, file_sha256, row_count) VALUES (?, ?, 0)",
            (f"manual-review-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}", batch_hash),
        )
        batch_id = batch_cursor.lastrowid
        connection.commit()

        print(f"{len(pending)} transaction(s) need review. Choose a category, skip, or quit.\n")
        added_examples = 0
        for position, transaction in enumerate(pending, start=1):
            current = transaction["current_category"] or "Uncategorised"
            print(f"{position}/{len(pending)}  {transaction['transaction_date']}  {money(transaction['amount_pence'])}  {transaction['account_name']}")
            print(f"  {transaction['raw_description']}")
            print(f"  Current: {current} ({transaction['category_source']})")
            for index, category in enumerate(categories, start=1):
                print(f"  {index}. {category['name']} [{category['kind']}]")
            while True:
                choice = input("Category number, s to skip, q to quit: ").strip().casefold()
                if choice in {"s", "skip"}:
                    print("Skipped.\n")
                    break
                if choice in {"q", "quit"}:
                    if added_examples == 0:
                        connection.execute("DELETE FROM training_imports WHERE training_import_id = ?", (batch_id,))
                        connection.commit()
                    print(f"Stopped. Added {added_examples} new training example(s) this session.")
                    return 0
                if choice.isdigit() and 1 <= int(choice) <= len(categories):
                    category = categories[int(choice) - 1]
                    added = save_choice(
                        connection,
                        transaction,
                        category["category_id"],
                        category["name"],
                        batch_id,
                        added_examples + 1,
                    )
                    added_examples += int(added)
                    suffix = "and added it to training examples" if added else "and updated its existing training example"
                    print(f"Saved as {category['name']} {suffix}.\n")
                    break
                print("Enter one of the listed numbers, s, or q.")

        if added_examples == 0:
            connection.execute("DELETE FROM training_imports WHERE training_import_id = ?", (batch_id,))
            connection.commit()
        print(f"Review complete. Added {added_examples} new training example(s).")
        return 0
    except (sqlite3.Error, EOFError, KeyboardInterrupt) as exc:
        if isinstance(exc, (EOFError, KeyboardInterrupt)):
            print("Review interrupted; saved choices are kept.")
            return 0
        print(f"Database error: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
