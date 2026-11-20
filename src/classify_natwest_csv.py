#!/usr/bin/env python3
"""Classify a NatWest CSV for review without importing or changing the database."""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from classifier_service import make_model, model_text, training_sample_weights
from import_natwest import DEFAULT_DATABASE, parse_csv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def normalise(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def transaction_key(transaction) -> tuple[str, int, str, str]:
    return (
        transaction.transaction_date,
        transaction.amount_pence,
        normalise(transaction.account_name),
        normalise(transaction.description),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, help="review CSV path (default: data/classification-review-<input name>.csv)")
    args = parser.parse_args()

    source = args.csv_file.expanduser().resolve()
    database = args.database.expanduser().resolve()
    output = (args.output.expanduser().resolve() if args.output else
              PROJECT_ROOT / "data" / f"classification-review-{source.stem}.csv")
    if not source.is_file():
        parser.error(f"Input CSV not found: {source}")
    if not database.is_file():
        parser.error(f"local-finance-tracker database not found: {database}")
    if output.exists():
        parser.error(f"Output already exists; choose another with --output: {output}")

    try:
        transactions, _ = parse_csv(source)
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            examples = connection.execute(
                """SELECT e.transaction_date, e.description, e.description_key,
                          e.amount_pence, e.account_name_key, e.label_origin,
                          c.name AS category
                   FROM training_examples e JOIN categories c ON c.category_id=e.category_id
                   WHERE TRIM(e.description)<>'' AND TRIM(e.source_category)<>''"""
            ).fetchall()
        if len(examples) < 20:
            raise ValueError(f"Only {len(examples)} labelled examples in database; need at least 20")

        # Preserve manually reviewed labels over imported labels for duplicate exact keys.
        manual_keys: dict[tuple[str, int, str, str], set[str]] = defaultdict(set)
        for row in examples:
            if row["label_origin"] == "manual" and row["transaction_date"] is not None and row["amount_pence"] is not None and row["account_name_key"]:
                manual_keys[(row["transaction_date"], row["amount_pence"], row["account_name_key"], row["description_key"])].add(row["category"])
        train_rows = [row for row in examples if row["label_origin"] == "manual" or
                      (row["transaction_date"] is None or row["amount_pence"] is None or not row["account_name_key"] or
                       (row["transaction_date"], row["amount_pence"], row["account_name_key"], row["description_key"]) not in manual_keys)]
        labels = {row["category"] for row in train_rows}
        if len(labels) < 2:
            raise ValueError("Training data must contain at least two categories")

        exact: dict[tuple[str, int, str, str], set[str]] = defaultdict(set)
        for row in train_rows:
            if row["transaction_date"] is not None and row["amount_pence"] is not None and row["account_name_key"]:
                key = (row["transaction_date"], row["amount_pence"], row["account_name_key"], row["description_key"])
                exact[key].add(row["category"])

        model = make_model()
        model.fit([model_text(row["description"], row["amount_pence"] or 0) for row in train_rows],
                  [row["category"] for row in train_rows],
                  classifier__sample_weight=training_sample_weights(train_rows))

        unresolved = [i for i, transaction in enumerate(transactions) if len(exact.get(transaction_key(transaction), set())) != 1]
        predicted = {}
        if unresolved:
            texts = [model_text(transactions[i].description, transactions[i].amount_pence) for i in unresolved]
            classes = model.predict(texts)
            probabilities = model.predict_proba(texts).max(axis=1)
            predicted = {i: (str(category), float(confidence)) for i, category, confidence in zip(unresolved, classes, probabilities)}

        output.parent.mkdir(parents=True, exist_ok=True)
        fields = ["Source row", "Date", "Type", "Description", "Value", "Account Name",
                  "Predicted Category", "Confidence", "Method", "Review Status"]
        methods = Counter()
        category_counts = Counter()
        with output.open("w", newline="", encoding="utf-8-sig") as target:
            writer = csv.DictWriter(target, fieldnames=fields)
            writer.writeheader()
            for index, transaction in enumerate(transactions):
                key_labels = exact.get(transaction_key(transaction), set())
                if len(key_labels) == 1:
                    category = next(iter(key_labels))
                    confidence = ""
                    method = "exact training lookup"
                else:
                    category, score = predicted[index]
                    confidence = f"{score:.3f}"
                    method = "local text + direction model" if not key_labels else "model (exact lookup conflict)"
                methods[method] += 1
                category_counts[category] += 1
                writer.writerow({
                    "Source row": transaction.source_row_number,
                    "Date": transaction.transaction_date,
                    "Type": transaction.transaction_type,
                    "Description": transaction.description,
                    "Value": f"{transaction.amount_pence / 100:.2f}",
                    "Account Name": transaction.account_name,
                    "Predicted Category": category,
                    "Confidence": confidence,
                    "Method": method,
                    "Review Status": "UNREVIEWED",
                })
        if os.name == "posix":
            try:
                os.chmod(output, 0o600)
            except OSError:
                pass

        print("Classification suggestions created (no database or source-file changes).")
        print(f"Input transactions: {len(transactions)}")
        print(f"Training examples used: {len(train_rows)}")
        print("By method: " + "; ".join(f"{name}: {count}" for name, count in sorted(methods.items())))
        print("Predicted category counts: " + "; ".join(f"{name}: {count}" for name, count in sorted(category_counts.items())))
        print(f"Review file: {output}")
        print("Confidence values are uncalibrated suggestions; no prediction is marked confirmed.")
        return 0
    except (ValueError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
