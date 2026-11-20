#!/usr/bin/env python3
"""Preview and import NatWest CSV transaction exports into local-finance-tracker's SQLite DB."""

from __future__ import annotations

import argparse
import csv
import hashlib
import sqlite3
import sys
import unicodedata
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from description_cleaner import clean_description


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "local-finance-tracker.sqlite"
REQUIRED_COLUMNS = {
    "Date",
    "Type",
    "Description",
    "Value",
    "Balance",
    "Account Name",
    "Account Number",
}


@dataclass(frozen=True)
class Transaction:
    source_row_number: int
    transaction_date: str
    transaction_type: str
    description: str
    amount_pence: int
    balance_pence: int
    account_name: str
    account_number: str


def pounds_to_pence(value: str | None, *, field: str, row_number: int) -> int:
    try:
        amount = Decimal((value or "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"Row {row_number}: invalid {field} amount {value!r}") from exc
    if not amount.is_finite() or amount.as_tuple().exponent < -2:
        raise ValueError(f"Row {row_number}: {field} must be a finite amount with at most 2 decimal places")
    return int(amount * 100)


def parse_csv(path: Path) -> tuple[list[Transaction], str]:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)

    transactions: list[Transaction] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                raise ValueError("The file is empty or has no CSV header")
            columns = {column.strip() for column in reader.fieldnames}
            missing = REQUIRED_COLUMNS - columns
            if missing:
                raise ValueError("Missing required CSV columns: " + ", ".join(sorted(missing)))

            for row_number, row in enumerate(reader, start=2):
                if None in row:
                    raise ValueError(f"Row {row_number}: more values than the CSV header")
                if not any((value or "").strip() for value in row.values()):
                    continue
                # Use stripped header names, so harmless whitespace around a header is accepted.
                clean_row = {(key or "").strip(): value for key, value in row.items()}
                try:
                    parsed_date = datetime.strptime(clean_row["Date"].strip(), "%d %b %Y").date()
                except (ValueError, AttributeError) as exc:
                    raise ValueError(f"Row {row_number}: invalid date {clean_row.get('Date')!r}; expected e.g. '21 Sep 2026'") from exc

                account_name = (clean_row.get("Account Name") or "").strip()
                account_number = (clean_row.get("Account Number") or "").strip()
                description = (clean_row.get("Description") or "").strip()
                transaction_type = (clean_row.get("Type") or "").strip()
                missing_fields = [
                    label for label, value in (
                        ("Account Name", account_name),
                        ("Account Number", account_number),
                        ("Description", description),
                    ) if not value
                ]
                if missing_fields:
                    raise ValueError(
                        f"Row {row_number}: required field(s) blank: " + ", ".join(missing_fields)
                    )
                # NatWest sometimes emits reversal/duplicate ledger rows without a Type.
                # Keep the financial row and mark the missing metadata explicitly; never
                # guess a transaction type from its description.
                if not transaction_type:
                    transaction_type = "Unspecified"

                transactions.append(
                    Transaction(
                        source_row_number=row_number,
                        transaction_date=parsed_date.isoformat(),
                        transaction_type=transaction_type,
                        description=description,
                        amount_pence=pounds_to_pence(clean_row["Value"], field="Value", row_number=row_number),
                        balance_pence=pounds_to_pence(clean_row["Balance"], field="Balance", row_number=row_number),
                        account_name=account_name,
                        account_number=account_number,
                    )
                )
    except UnicodeDecodeError as exc:
        raise ValueError("Could not read this file as UTF-8 CSV") from exc

    if not transactions:
        raise ValueError("No transaction rows found")
    return transactions, digest.hexdigest()


def money(pence: int) -> str:
    return f"£{pence / 100:,.2f}"


def masked_account(number: str) -> str:
    return f"…{number[-4:]}" if len(number) >= 4 else "…"


def normalise(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def build_merchant_training_lookup(connection: sqlite3.Connection) -> dict[str, int]:
    """Build high-purity merchant labels from the training period only."""
    available = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'training_examples'"
    ).fetchone()
    if not available:
        return {}
    rows = connection.execute(
        """SELECT transaction_date, amount_pence, account_name_key, description_key,
                  label_origin, description, category_id
           FROM training_examples
           WHERE TRIM(description)<>'' AND TRIM(source_category)<>''"""
    ).fetchall()
    manual_keys = {
        (row[0], row[1], row[2], row[3])
        for row in rows
        if row[4] == "manual" and row[0] and row[1] is not None and row[2]
    }
    counts: dict[str, Counter[int]] = defaultdict(Counter)
    for row in rows:
        key = (row[0], row[1], row[2], row[3])
        if row[4] != "manual" and row[0] and row[1] is not None and row[2] and key in manual_keys:
            continue
        merchant = clean_description(row[5])
        if merchant:
            counts[merchant][row[6]] += 1

    matches: dict[str, int] = {}
    for merchant, category_counts in counts.items():
        category_id, support = category_counts.most_common(1)[0]
        total = sum(category_counts.values())
        if total >= 2 and support / total >= 0.95:
            matches[merchant] = category_id
    return matches


def apply_merchant_training_matches(connection: sqlite3.Connection) -> int:
    """Resolve pending rows whose cleaned merchant has a high-agreement reviewed label."""
    merchant_lookup = build_merchant_training_lookup(connection)
    if not merchant_lookup:
        return 0
    rows = connection.execute(
        """SELECT t.transaction_id,t.transaction_date,t.raw_description,t.amount_pence,a.account_name
           FROM transactions t JOIN accounts a USING(account_id)
           WHERE (review_required=1 OR category_id IS NULL)
             AND t.category_source IN ('unreviewed','model','training_lookup')"""
    ).fetchall()
    resolved = []
    for row in rows:
        transaction = Transaction(
            1, row["transaction_date"], "", row["raw_description"],
            row["amount_pence"], 0, row["account_name"], "",
        )
        category_id, match_state = training_lookup(connection, transaction, merchant_lookup)
        if match_state == "merchant_hit":
            resolved.append((category_id, row["transaction_id"]))
    if not resolved:
        return 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.executemany(
            """UPDATE transactions SET category_id=?,category_source='training_lookup',
                   review_required=0,classification_confidence=NULL,classification_model=NULL
               WHERE transaction_id=?""",
            resolved,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return len(resolved)


def training_lookup(
    connection: sqlite3.Connection,
    transaction: Transaction,
    merchant_lookup: dict[str, int] | None = None,
) -> tuple[int | None, str]:
    """Return category ID and state: hit, miss, ambiguous, or unavailable."""
    available = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'training_examples'"
    ).fetchone()
    if not available:
        return None, "unavailable"
    rows = connection.execute(
        """SELECT DISTINCT category_id, label_origin = 'manual' AS manually_confirmed
           FROM training_examples
           WHERE transaction_date = ? AND amount_pence = ?
             AND account_name_key = ? AND description_key = ?""",
        (
            transaction.transaction_date,
            transaction.amount_pence,
            normalise(transaction.account_name),
            normalise(transaction.description),
        ),
    ).fetchall()
    confirmed_rows = [row for row in rows if row[1]]
    selected_rows = confirmed_rows if confirmed_rows else rows
    category_ids = {row[0] for row in selected_rows}
    if len(category_ids) == 1:
        return next(iter(category_ids)), "hit"
    if len(category_ids) > 1:
        return None, "ambiguous"
    if merchant_lookup is not None:
        merchant_category = merchant_lookup.get(clean_description(transaction.description))
        if merchant_category is not None:
            return merchant_category, "merchant_hit"
    return None, "miss"


def remove_already_imported_rows(
    connection: sqlite3.Connection, transactions: list[Transaction]
) -> tuple[list[Transaction], int]:
    """Filter row-for-row overlap using account, full transaction fields, and occurrence counts."""
    existing: dict[tuple[str, str, str, str, int, int], int] = {}
    rows = connection.execute(
        """SELECT a.account_number,t.transaction_date,t.transaction_type,t.raw_description,
                  t.amount_pence,t.balance_pence,COUNT(*)
           FROM transactions t JOIN accounts a USING(account_id)
           GROUP BY a.account_number,t.transaction_date,t.transaction_type,t.raw_description,
                    t.amount_pence,t.balance_pence"""
    ).fetchall()
    for row in rows:
        existing[(row[0], row[1], row[2], normalise(row[3]), row[4], row[5] or 0)] = row[6]

    seen: Counter[tuple[str, str, str, str, int, int]] = Counter()
    retained: list[Transaction] = []
    skipped = 0
    for transaction in transactions:
        key = (
            transaction.account_number,
            transaction.transaction_date,
            transaction.transaction_type,
            normalise(transaction.description),
            transaction.amount_pence,
            transaction.balance_pence,
        )
        seen[key] += 1
        if seen[key] <= existing.get(key, 0):
            skipped += 1
        else:
            retained.append(transaction)
    return retained, skipped


TRANSFER_CUES = ("transfer", "faster payment", "own account", "internal transfer")
TRANSFER_TYPE_CODES = {"tfr", "fpo", "fpi", "fps"}


def looks_like_bank_transfer(description: str, transaction_type: str) -> bool:
    text = normalise(f"{description} {transaction_type}")
    tokens = set(re.findall(r"[a-z0-9]+", text))
    return bool(tokens & TRANSFER_TYPE_CODES) or any(cue in text for cue in TRANSFER_CUES)


def detect_internal_transfer_pairs(
    connection: sqlite3.Connection, transactions: list[Transaction]
) -> tuple[set[int], set[int]]:
    """Find unambiguous opposite-value transfer pairs on different accounts and dates.

    NatWest CSVs contain dates but no transaction times, so the strongest available
    time match is the same transaction date. Only unique one-to-one matches qualify.
    Returns incoming CSV row numbers and existing transaction IDs.
    """
    candidates: dict[tuple[str, int], dict[str, object]] = {}
    for transaction in transactions:
        if transaction.amount_pence == 0 or not looks_like_bank_transfer(transaction.description, transaction.transaction_type):
            continue
        key = ("new", transaction.source_row_number)
        candidates[key] = {
            "account": transaction.account_number,
            "date": transaction.transaction_date,
            "amount": transaction.amount_pence,
            "description": transaction.description,
            "type": transaction.transaction_type,
        }

    if not candidates:
        return set(), set()

    placeholders = ",".join("?" for _ in {item["date"] for item in candidates.values()})
    dates = sorted({item["date"] for item in candidates.values()})
    existing_rows = connection.execute(
        f"""SELECT t.transaction_id,a.account_number,t.transaction_date,t.amount_pence,
                   t.raw_description,t.transaction_type,t.category_source,c.kind
            FROM transactions t JOIN accounts a USING(account_id)
            LEFT JOIN categories c USING(category_id)
            WHERE t.transaction_date IN ({placeholders}) AND t.amount_pence<>0
              AND t.category_source IN ('unreviewed','model','training_lookup')
              AND COALESCE(c.kind,'')<>'transfer'""",
        dates,
    ).fetchall()
    for row in existing_rows:
        if not looks_like_bank_transfer(row[4], row[5]):
            continue
        candidates[("old", row[0])] = {
            "account": row[1], "date": row[2], "amount": row[3],
            "description": row[4], "type": row[5],
        }

    adjacency: dict[tuple[str, int], set[tuple[str, int]]] = {key: set() for key in candidates}
    keys = list(candidates)
    for index, left_key in enumerate(keys):
        left = candidates[left_key]
        for right_key in keys[index + 1:]:
            right = candidates[right_key]
            if left_key[0] == right_key[0] == "old":
                continue
            if (left["date"] == right["date"] and left["account"] != right["account"]
                    and left["amount"] == -right["amount"]):
                adjacency[left_key].add(right_key)
                adjacency[right_key].add(left_key)

    incoming_rows: set[int] = set()
    existing_ids: set[int] = set()
    for key, matches in adjacency.items():
        if len(matches) != 1:
            continue
        other = next(iter(matches))
        if len(adjacency[other]) != 1:
            continue
        if key[0] == "new":
            incoming_rows.add(key[1])
        else:
            existing_ids.add(key[1])
    return incoming_rows, existing_ids


def show_preview(path: Path, transactions: list[Transaction], digest: str, database: Path) -> bool:
    print(f"File: {path.name}")
    print(f"Rows: {len(transactions)}")
    print(f"Date range: {min(t.transaction_date for t in transactions)} to {max(t.transaction_date for t in transactions)}")
    print(f"SHA-256: {digest}")

    by_account: dict[tuple[str, str], list[Transaction]] = defaultdict(list)
    for transaction in transactions:
        by_account[(transaction.account_name, transaction.account_number)].append(transaction)
    print("Accounts:")
    for (name, number), rows in sorted(by_account.items()):
        net = sum(row.amount_pence for row in rows)
        print(f"  {name} ({masked_account(number)}): {len(rows)} rows, net {money(net)}")

    types = Counter(t.transaction_type for t in transactions)
    print("Transaction types: " + ", ".join(f"{name}={count}" for name, count in sorted(types.items())))
    if types["Unspecified"]:
        print(f"Note: {types['Unspecified']} row(s) had a blank Type; retained with Type set to 'Unspecified'.")
    print("Sample rows:")
    for t in transactions[:5]:
        print(f"  {t.transaction_date} | {money(t.amount_pence)} | {t.account_name} | {t.description}")

    if database.exists():
        with sqlite3.connect(database) as connection:
            duplicate = connection.execute(
                "SELECT import_id FROM imports WHERE file_sha256 = ?", (digest,)
            ).fetchone()
            if not duplicate:
                merchant_lookup = build_merchant_training_lookup(connection)
                results = Counter(training_lookup(connection, transaction, merchant_lookup)[1] for transaction in transactions)
                if results["unavailable"] == len(transactions):
                    print("Training lookup: not set up yet")
                else:
                    print(
                        f"Exact training matches: {results['hit']}; consistent merchant matches: {results['merchant_hit']}; "
                        f"conflicting exact matches: {results['ambiguous']}"
                    )
        if duplicate:
            print(f"\nThis exact file was already imported (import ID {duplicate[0]}). No changes made.")
            return True
    return False


def commit_import(
    path: Path,
    database: Path,
    transactions: list[Transaction],
    digest: str,
    suggestions: dict[int, tuple[int, float]] | None = None,
    model_name: str | None = None,
    transfer_rows: set[int] | None = None,
    transfer_existing_ids: set[int] | None = None,
) -> int:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        if transfer_rows is None or transfer_existing_ids is None:
            detected_rows, detected_existing = detect_internal_transfer_pairs(connection, transactions)
            transfer_rows = detected_rows if transfer_rows is None else transfer_rows
            transfer_existing_ids = detected_existing if transfer_existing_ids is None else transfer_existing_ids
        else:
            transfer_rows = set(transfer_rows)
            transfer_existing_ids = set(transfer_existing_ids)

        transfer_category = None
        if transfer_rows or transfer_existing_ids:
            transfer_category = connection.execute(
                "SELECT category_id FROM categories WHERE name='Transfer' AND kind='transfer'"
            ).fetchone()
            if transfer_category is None:
                raise ValueError("Automatic transfer detection needs a 'Transfer' category with kind='transfer'")
            transfer_category_id = transfer_category[0]
            if transfer_existing_ids:
                connection.executemany(
                    """UPDATE transactions SET category_id=?,category_source='rule',review_required=0,
                              classification_confidence=NULL,classification_model=NULL
                       WHERE transaction_id=? AND category_source IN ('unreviewed','model','training_lookup')""",
                    [(transfer_category_id, transaction_id) for transaction_id in sorted(transfer_existing_ids)],
                )
        existing = connection.execute(
            "SELECT import_id FROM imports WHERE file_sha256 = ?", (digest,)
        ).fetchone()
        if existing:
            raise ValueError(f"This exact file was already imported as import ID {existing[0]}")

        cursor = connection.execute(
            "INSERT INTO imports (filename, file_sha256, row_count) VALUES (?, ?, ?)",
            (path.name, digest, len(transactions)),
        )
        import_id = cursor.lastrowid
        merchant_lookup = build_merchant_training_lookup(connection)

        account_ids: dict[str, int] = {}
        for transaction in transactions:
            number = transaction.account_number
            if number not in account_ids:
                connection.execute(
                    "INSERT INTO accounts (account_number, account_name) VALUES (?, ?) "
                    "ON CONFLICT(account_number) DO UPDATE SET account_name = excluded.account_name",
                    (number, transaction.account_name),
                )
                account_ids[number] = connection.execute(
                    "SELECT account_id FROM accounts WHERE account_number = ?", (number,)
                ).fetchone()[0]

            category_id, match_state = training_lookup(connection, transaction, merchant_lookup)
            has_training_match = match_state in ("hit", "merchant_hit")
            is_transfer = transaction.source_row_number in transfer_rows
            connection.execute(
                """INSERT INTO transactions (
                       account_id, import_id, source_row_number, transaction_date,
                       transaction_type, raw_description, amount_pence, balance_pence,
                       category_id, category_source, review_required,
                       classification_confidence, classification_model
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    account_ids[number],
                    import_id,
                    transaction.source_row_number,
                    transaction.transaction_date,
                    transaction.transaction_type,
                    transaction.description,
                    transaction.amount_pence,
                    transaction.balance_pence,
                    transfer_category_id if is_transfer else category_id if has_training_match else (
                        suggestions.get(transaction.source_row_number, (None, None))[0] if suggestions else None
                    ),
                    "rule" if is_transfer else "training_lookup" if has_training_match else (
                        "model" if suggestions and transaction.source_row_number in suggestions else "unreviewed"
                    ),
                    0 if is_transfer or has_training_match else 1,
                    None if is_transfer or has_training_match or not suggestions or transaction.source_row_number not in suggestions
                    else suggestions[transaction.source_row_number][1],
                    None if is_transfer or has_training_match or not suggestions or transaction.source_row_number not in suggestions
                    else model_name,
                ),
            )
        connection.commit()
        return import_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", type=Path, help="NatWest CSV file to preview or import")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help=f"SQLite database (default: {DEFAULT_DATABASE})")
    parser.add_argument("--commit", action="store_true", help="Import after showing the preview and receiving confirmation")
    args = parser.parse_args()

    path = args.csv_file.expanduser().resolve()
    database = args.database.expanduser().resolve()
    if not path.is_file():
        print(f"Error: CSV file not found: {path}", file=sys.stderr)
        return 2

    try:
        transactions, digest = parse_csv(path)
        already_imported = show_preview(path, transactions, digest, database)
        if already_imported or not args.commit:
            if not args.commit and not already_imported:
                print("\nPreview only. Re-run with --commit to import these rows.")
            return 0

        answer = input("\nType IMPORT to save these rows to SQLite: ").strip()
        if answer != "IMPORT":
            print("Import cancelled; database unchanged.")
            return 0

        import_id = commit_import(path, database, transactions, digest)
        print(f"Imported {len(transactions)} transactions (import ID {import_id}).")
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
