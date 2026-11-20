#!/usr/bin/env python3
"""Evaluate exact training lookup followed by a local transaction-text classifier."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from sklearn.metrics import accuracy_score, classification_report, f1_score

from classifier_service import make_model, training_sample_weights
from description_cleaner import clean_description


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "local-finance-tracker.sqlite"


def transaction_key(row: sqlite3.Row) -> tuple[str, int, str, str] | None:
    if row["transaction_date"] is None or row["amount_pence"] is None or row["account_name_key"] is None:
        return None
    return (
        row["transaction_date"],
        row["amount_pence"],
        row["account_name_key"],
        row["description_key"],
    )


def chronological_split(rows: list[sqlite3.Row], test_fraction: float):
    dated_counts = Counter(row["transaction_date"] for row in rows if row["transaction_date"] is not None)
    dates = sorted(dated_counts)
    if len(dates) < 2:
        raise ValueError("Need examples from at least two different dates for a date-based holdout")

    target_train_size = sum(dated_counts.values()) * (1.0 - test_fraction)
    train_dates: set[str] = set()
    train_size = 0
    for transaction_date in dates:
        if train_dates and train_size + dated_counts[transaction_date] > target_train_size:
            break
        if not train_dates and len(dates) > 1 and train_size + dated_counts[transaction_date] >= target_train_size:
            # Keep at least one complete date in the test set.
            train_dates.add(transaction_date)
            train_size += dated_counts[transaction_date]
            continue
        train_dates.add(transaction_date)
        train_size += dated_counts[transaction_date]

    # The normal case exits on the first date that would cross the target. Ensure both sets exist.
    if len(train_dates) >= len(dates):
        train_dates = set(dates[:-1])
    test_dates = [transaction_date for transaction_date in dates if transaction_date not in train_dates]
    cutoff = test_dates[0]
    train_rows = [row for row in rows if row["transaction_date"] is None or row["transaction_date"] < cutoff]
    test_rows = [row for row in rows if row["transaction_date"] is not None and row["transaction_date"] >= cutoff]
    return train_rows, test_rows, cutoff


def moneyless_metrics(y_true: list[str], y_pred: list[str]) -> None:
    if not y_true:
        print("No transactions to score.")
        return
    print(f"  Rows scored: {len(y_true)}")
    print(f"  Accuracy: {accuracy_score(y_true, y_pred):.3f}")
    print(f"  Macro F1: {f1_score(y_true, y_pred, average='macro', zero_division=0):.3f}")
    print("  Per-category precision / recall / F1 / support:")
    print(classification_report(y_true, y_pred, zero_division=0, digits=3))


def print_largest_confusions(y_true: list[str], y_pred: list[str], limit: int = 12) -> None:
    confusions = Counter(
        (truth, predicted)
        for truth, predicted in zip(y_true, y_pred)
        if truth != predicted
    )
    print("Most common errors (actual -> predicted):")
    if not confusions:
        print("  None")
        return
    for (truth, predicted), count in confusions.most_common(limit):
        print(f"  {truth} -> {predicted}: {count}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--test-fraction", type=float, default=0.20)
    parser.add_argument(
        "--text-mode",
        choices=("raw", "cleaned", "combined", "raw+direction", "compare"),
        default="compare",
        help="choose description features or compare raw, cleaned, combined, and amount direction",
    )
    args = parser.parse_args()
    if not 0.05 <= args.test_fraction <= 0.5:
        parser.error("--test-fraction must be between 0.05 and 0.5")

    database = args.database.expanduser().resolve()
    if not database.is_file():
        print(f"Database not found: {database}", file=sys.stderr)
        return 2

    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT e.transaction_date, e.description, e.description_key,
                          e.amount_pence, e.account_name_key, e.source_transaction_id,
                          e.label_origin,
                          c.name AS category
                   FROM training_examples AS e
                   JOIN categories AS c ON c.category_id = e.category_id
                   WHERE TRIM(e.description) <> '' AND TRIM(e.source_category) <> ''"""
            ).fetchall()
        if len(rows) < 20:
            raise ValueError(f"Only {len(rows)} labelled examples found; need at least 20 for this evaluation")

        train_rows, test_rows, cutoff = chronological_split(rows, args.test_fraction)
        if not train_rows or not test_rows:
            raise ValueError("The date split produced an empty training or test set")

        print("local-finance-tracker local classifier evaluation")
        print("=" * 34)
        print(f"Labelled examples: {len(rows)}")
        print(f"Training examples: {len(train_rows)}")
        print(f"Test examples: {len(test_rows)}")
        print(f"Test begins: {cutoff} (all rows on a date stay in the same split)")
        print("Training examples without a date are included in training only.")

        train_counts = Counter(row["category"] for row in train_rows)
        test_counts = Counter(row["category"] for row in test_rows)
        print("\nTraining support by category:")
        for category, count in sorted(train_counts.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {category}: {count}")
        unseen = sorted(set(test_counts) - set(train_counts))
        if unseen:
            print("Categories with no training examples (model cannot learn these yet): " + ", ".join(unseen))

        lookup_labels: dict[tuple[str, int, str, str], set[str]] = defaultdict(set)
        manually_confirmed_labels: dict[tuple[str, int, str, str], set[str]] = defaultdict(set)
        for row in train_rows:
            key = transaction_key(row)
            if key is not None:
                if row["label_origin"] == "manual":
                    manually_confirmed_labels[key].add(row["category"])
                else:
                    lookup_labels[key].add(row["category"])

        # In production, a reviewed label overrides an older imported label for the same exact row.
        effective_train_rows = [
            row
            for row in train_rows
            if row["label_origin"] == "manual"
            or transaction_key(row) not in manually_confirmed_labels
        ]
        if len(effective_train_rows) != len(train_rows):
            print(f"Historical labels superseded by manual corrections: {len(train_rows) - len(effective_train_rows)}")

        exact_true: list[str] = []
        exact_pred: list[str] = []
        model_rows: list[sqlite3.Row] = []
        ambiguous_count = 0
        for row in test_rows:
            key = transaction_key(row)
            labels = set()
            if key is not None:
                labels = manually_confirmed_labels.get(key) or lookup_labels.get(key, set())
            if len(labels) == 1:
                exact_true.append(row["category"])
                exact_pred.append(next(iter(labels)))
            elif len(labels) > 1:
                ambiguous_count += 1
            else:
                model_rows.append(row)

        print("\nExact lookup on held-out transactions:")
        print(f"  Unique exact matches: {len(exact_true)}")
        print(f"  Conflicting exact matches (left for review): {ambiguous_count}")
        print(f"  Lookup misses passed to classifier: {len(model_rows)}")
        if exact_true:
            print(f"  Exact lookup accuracy: {accuracy_score(exact_true, exact_pred):.3f}")

        if not model_rows:
            print("\nNo lookup misses to evaluate with the classifier.")
            return 0

        y_true = [row["category"] for row in model_rows]
        text_builders = {
            "raw": lambda row: row["description"],
            "cleaned": lambda row: clean_description(row["description"]),
            "combined": lambda row: row["description"] + " merchant " + clean_description(row["description"]),
            "raw+direction": lambda row: row["description"] + " transaction_direction " + (
                "inflow" if row["amount_pence"] is not None and row["amount_pence"] > 0
                else "outflow" if row["amount_pence"] is not None and row["amount_pence"] < 0
                else "zero_or_unknown"
            ),
        }
        modes = list(text_builders) if args.text_mode == "compare" else [args.text_mode]
        for mode in modes:
            build_text = text_builders[mode]
            model = make_model()
            model.fit(
                [build_text(row) for row in effective_train_rows],
                [row["category"] for row in effective_train_rows],
                classifier__sample_weight=training_sample_weights(effective_train_rows),
            )
            texts = [build_text(row) for row in model_rows]
            y_pred = model.predict(texts).tolist()
            confidence = model.predict_proba(texts).max(axis=1)

            print(f"\nClassifier results using {mode} descriptions:")
            moneyless_metrics(y_true, y_pred)
            print_largest_confusions(y_true, y_pred)
            print("Confidence review thresholds (exploratory; probabilities are not calibrated):")
            for threshold in (0.50, 0.60, 0.70, 0.80, 0.90):
                accepted = [index for index, value in enumerate(confidence) if value >= threshold]
                if accepted:
                    accepted_accuracy = accuracy_score(
                        [y_true[index] for index in accepted],
                        [y_pred[index] for index in accepted],
                    )
                    print(
                        f"  >= {threshold:.2f}: {len(accepted)}/{len(y_true)} accepted "
                        f"({len(accepted) / len(y_true):.1%} coverage), accuracy {accepted_accuracy:.3f}"
                    )
                else:
                    print(f"  >= {threshold:.2f}: 0/{len(y_true)} accepted (0.0% coverage)")

            combined_true = exact_true + y_true
            combined_pred = exact_pred + y_pred
            print("Combined pipeline on resolvable held-out transactions:")
            moneyless_metrics(combined_true, combined_pred)
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Evaluation error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
