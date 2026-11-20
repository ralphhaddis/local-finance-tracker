#!/usr/bin/env python3
"""Review and correct wrong predictions in the chronological model holdout."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import threading
from collections import defaultdict
from pathlib import Path

from description_cleaner import clean_description
from classifier_service import training_sample_weights
from evaluate_classifier import chronological_split, make_model, transaction_key


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "local-finance-tracker.sqlite"
_holdout_prediction_cache: dict[tuple[str, float, str], dict[int, tuple[str, float]]] = {}
_holdout_cache_lock = threading.Lock()


def model_text(row: sqlite3.Row, mode: str) -> str:
    raw = row["description"]
    cleaned = clean_description(raw)
    if mode == "raw":
        return raw
    if mode == "cleaned":
        return cleaned
    return raw + " merchant " + cleaned


def classify_holdout(connection: sqlite3.Connection, test_fraction: float, mode: str):
    rows = connection.execute(
        """SELECT e.example_id, e.transaction_date, e.description, e.description_key,
                  e.amount_pence, e.account_name_key, e.source_transaction_id,
                  e.label_origin, e.original_category_id, e.reviewed_at,
                  e.category_id, c.name AS category,
                  a.account_name
           FROM training_examples AS e
           JOIN categories AS c ON c.category_id = e.category_id
           LEFT JOIN transactions AS t ON t.transaction_id = e.source_transaction_id
           LEFT JOIN accounts AS a ON a.account_id = t.account_id
           WHERE TRIM(e.description) <> '' AND TRIM(e.source_category) <> ''"""
    ).fetchall()
    train_rows, test_rows, cutoff = chronological_split(rows, test_fraction)
    if len(train_rows) < 20 or not test_rows:
        raise ValueError("The chronological split does not have enough examples to review")

    manual_labels = defaultdict(set)
    imported_labels = defaultdict(set)
    for row in train_rows:
        key = transaction_key(row)
        if key is None:
            continue
        target = manual_labels if row["label_origin"] == "manual" else imported_labels
        target[key].add(row["category"])

    effective_train_rows = [
        row
        for row in train_rows
        if row["label_origin"] == "manual" or transaction_key(row) not in manual_labels
    ]
    misses = []
    ambiguous = 0
    exact = 0
    for row in test_rows:
        key = transaction_key(row)
        labels = set()
        if key is not None:
            labels = manual_labels.get(key) or imported_labels.get(key, set())
        if len(labels) == 1:
            exact += 1
            continue
        if len(labels) > 1:
            ambiguous += 1
            continue
        misses.append(row)

    fingerprint = hashlib.sha256()
    fingerprint.update(f"{test_fraction!r}:{mode}:".encode())
    for row in sorted(rows, key=lambda item: item["example_id"]):
        fingerprint.update(repr(tuple(row)).encode("utf-8"))
        fingerprint.update(b"\0")
    cache_key = (fingerprint.hexdigest(), test_fraction, mode)
    with _holdout_cache_lock:
        predictions_by_id = _holdout_prediction_cache.get(cache_key)
        if predictions_by_id is None:
            model = make_model()
            model.fit(
                [model_text(row, mode) for row in effective_train_rows],
                [row["category"] for row in effective_train_rows],
                classifier__sample_weight=training_sample_weights(effective_train_rows),
            )
            if misses:
                texts = [model_text(row, mode) for row in misses]
                predictions = model.predict(texts).tolist()
                probabilities = model.predict_proba(texts).max(axis=1)
                predictions_by_id = {
                    row["example_id"]: (prediction, float(confidence))
                    for row, prediction, confidence in zip(misses, predictions, probabilities)
                }
            else:
                predictions_by_id = {}
            if len(_holdout_prediction_cache) >= 2:
                _holdout_prediction_cache.pop(next(iter(_holdout_prediction_cache)))
            _holdout_prediction_cache[cache_key] = predictions_by_id
    errors = [
        (row, prediction, float(confidence))
        for row in misses
        for prediction, confidence in [predictions_by_id[row["example_id"]]]
        if row["category"] != prediction
    ]
    already_reviewed = {
        row[0] for row in connection.execute(
            "SELECT DISTINCT example_id FROM error_review_decisions WHERE undone_at IS NULL"
        )
    }
    errors = [item for item in errors if item[0]["example_id"] not in already_reviewed]
    return errors, len(test_rows), len(misses), exact, ambiguous, cutoff


def record_label(connection: sqlite3.Connection, row: sqlite3.Row, category_id: int) -> int:
    connection.execute("BEGIN IMMEDIATE")
    try:
        old = connection.execute(
            "SELECT category_id,label_origin,original_category_id,reviewed_at,source_transaction_id "
            "FROM training_examples WHERE example_id=?",
            (row["example_id"],),
        ).fetchone()
        if old is None:
            raise ValueError("Training example no longer exists")
        transaction_state = None
        if old["source_transaction_id"] is not None:
            transaction_state = connection.execute(
                "SELECT category_id,category_source,review_required FROM transactions WHERE transaction_id=?",
                (old["source_transaction_id"],),
            ).fetchone()
        cursor = connection.execute(
            """INSERT INTO error_review_decisions (
                   example_id,previous_category_id,chosen_category_id,previous_label_origin,
                   previous_original_category_id,previous_reviewed_at,source_transaction_id,
                   previous_transaction_category_id,previous_transaction_category_source,
                   previous_transaction_review_required
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (row["example_id"], old["category_id"], category_id, old["label_origin"],
             old["original_category_id"], old["reviewed_at"], old["source_transaction_id"],
             transaction_state["category_id"] if transaction_state else None,
             transaction_state["category_source"] if transaction_state else None,
             transaction_state["review_required"] if transaction_state else None),
        )
        decision_id = cursor.lastrowid
        connection.execute(
            """UPDATE training_examples
               SET original_category_id = COALESCE(original_category_id, category_id),
                   category_id = ?, label_origin = 'manual', reviewed_at = CURRENT_TIMESTAMP
               WHERE example_id = ?""",
            (category_id, row["example_id"]),
        )
        if row["source_transaction_id"] is not None:
            connection.execute(
                """UPDATE transactions
                   SET category_id = ?, category_source = 'manual', review_required = 0
                   WHERE transaction_id = ?""",
                (category_id, row["source_transaction_id"]),
            )
        connection.commit()
        return decision_id
    except Exception:
        connection.rollback()
        raise


def undo_label(connection: sqlite3.Connection, decision_id: int) -> sqlite3.Row:
    connection.execute("BEGIN IMMEDIATE")
    try:
        decision = connection.execute(
            """SELECT d.*,e.description,e.transaction_date,e.amount_pence
               FROM error_review_decisions d JOIN training_examples e USING(example_id)
               WHERE d.decision_id=? AND d.undone_at IS NULL""",
            (decision_id,),
        ).fetchone()
        if decision is None:
            raise ValueError("Decision is already undone or does not exist")
        later = connection.execute(
            """SELECT 1 FROM error_review_decisions
               WHERE example_id=? AND decision_id>? AND undone_at IS NULL LIMIT 1""",
            (decision["example_id"], decision_id),
        ).fetchone()
        if later:
            raise ValueError("Undo the newer decision for this example first")
        connection.execute(
            """UPDATE training_examples SET category_id=?,label_origin=?,
                   original_category_id=?,reviewed_at=? WHERE example_id=?""",
            (decision["previous_category_id"], decision["previous_label_origin"],
             decision["previous_original_category_id"], decision["previous_reviewed_at"],
             decision["example_id"]),
        )
        if decision["source_transaction_id"] is not None:
            connection.execute(
                """UPDATE transactions SET category_id=?,category_source=?,review_required=?
                   WHERE transaction_id=?""",
                (decision["previous_transaction_category_id"],
                 decision["previous_transaction_category_source"],
                 decision["previous_transaction_review_required"],
                 decision["source_transaction_id"]),
            )
        connection.execute(
            "UPDATE error_review_decisions SET undone_at=CURRENT_TIMESTAMP WHERE decision_id=?",
            (decision_id,),
        )
        connection.commit()
        return decision
    except Exception:
        connection.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--test-fraction", type=float, default=0.20)
    parser.add_argument("--text-mode", choices=("raw", "cleaned", "combined"), default="raw")
    args = parser.parse_args()
    if not 0.05 <= args.test_fraction <= 0.5:
        parser.error("--test-fraction must be between 0.05 and 0.5")

    database = args.database.expanduser().resolve()
    if not database.is_file():
        print(f"Database not found: {database}", file=sys.stderr)
        return 2

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        errors, test_count, model_count, exact_count, ambiguous_count, cutoff = classify_holdout(
            connection, args.test_fraction, args.text_mode
        )
        print(f"Holdout begins {cutoff}; {test_count} rows, {exact_count} exact matches, "
              f"{ambiguous_count} ambiguous, {model_count} sent to the model.")
        print(f"Misclassified model suggestions: {len(errors)}")
        if not errors:
            print("There are no model errors to review.")
            return 0

        categories = connection.execute(
            "SELECT category_id, name, kind FROM categories WHERE name <> 'Uncategorised' ORDER BY kind, name"
        ).fetchall()
        for position, (row, predicted, confidence) in enumerate(errors, start=1):
            print(f"\n{position}/{len(errors)}  {row['transaction_date']}  {row['account_name'] or 'training example'}")
            print(f"Description: {row['description']}")
            print(f"Current label: {row['category']}  |  model: {predicted}  |  confidence: {confidence:.3f}")
            print("  k. Confirm current label")
            print("  c. Correct label")
            print("  s. Skip")
            print("  q. Quit")
            while True:
                action = input("Action: ").strip().casefold()
                if action in {"s", "skip"}:
                    break
                if action in {"q", "quit"}:
                    print("Stopped. Saved label reviews are kept.")
                    return 0
                if action in {"k", "keep"}:
                    record_label(connection, row, row["category_id"])
                    print("Current label confirmed.")
                    break
                if action in {"c", "correct"}:
                    for index, category in enumerate(categories, start=1):
                        print(f"  {index}. {category['name']} [{category['kind']}]")
                    choice = input("Correct category number (or b to go back): ").strip().casefold()
                    if choice in {"b", "back"}:
                        continue
                    if choice.isdigit() and 1 <= int(choice) <= len(categories):
                        category = categories[int(choice) - 1]
                        record_label(connection, row, category["category_id"])
                        print(f"Label corrected to {category['name']}.")
                        break
                    print("Enter one of the category numbers or b.")
                    continue
                print("Choose k, c, s, or q.")
        print("Error review complete.")
        return 0
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"Review error: {exc}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Review interrupted. Saved label reviews are kept.")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
