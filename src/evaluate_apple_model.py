#!/usr/bin/env python3
"""Compare Apple Foundation Models with local-finance-tracker's local classifier on a date holdout."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.feature_extraction.text import TfidfVectorizer

from classifier_service import make_model, model_text, training_sample_weights
from description_cleaner import clean_description
from evaluate_classifier import chronological_split, transaction_key


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "local-finance-tracker.sqlite"
BRIDGE_PACKAGE = PROJECT_ROOT / "experiments" / "apple-foundation-model"
OUTPUT_CSV = PROJECT_ROOT / "data" / "apple-model-enhanced-evaluation.csv"


def write_report(title: str, truth: list[str], predictions: list[str], categories: list[str]) -> None:
    print(f"\n{title}")
    print(f"  Rows: {len(truth)}")
    print(f"  Accuracy: {accuracy_score(truth, predictions):.3f}")
    print(f"  Macro F1: {f1_score(truth, predictions, labels=categories, average='macro', zero_division=0):.3f}")
    print("  Per-category precision / recall / F1 / support:")
    print(classification_report(truth, predictions, labels=categories, zero_division=0, digits=3))
    confusions = Counter((actual, predicted) for actual, predicted in zip(truth, predictions) if actual != predicted)
    print("  Most common errors (actual -> predicted):")
    for (actual, predicted), count in confusions.most_common(10):
        print(f"    {actual} -> {predicted}: {count}")
    if not confusions:
        print("    None")


def nearest_examples(train_rows, queries, count: int) -> list[list[dict[str, object]]]:
    if count <= 0 or not train_rows:
        return [[] for _ in queries]
    vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), lowercase=True, strip_accents="unicode")
    train_vectors = vectorizer.fit_transform([clean_description(row["description"]) for row in train_rows])
    query_vectors = vectorizer.transform([clean_description(row["description"]) for row in queries])
    similarities = cosine_similarity(query_vectors, train_vectors)
    selected: list[list[dict[str, object]]] = []
    for row, scores in zip(queries, similarities):
        key = transaction_key(row)
        indices = sorted(range(len(train_rows)), key=lambda index: scores[index], reverse=True)
        examples = []
        seen: set[tuple[str, str]] = set()
        for index in indices:
            candidate = train_rows[index]
            if key is not None and transaction_key(candidate) == key:
                continue
            merchant_category = (candidate["description_key"], candidate["category"])
            if merchant_category in seen:
                continue
            seen.add(merchant_category)
            examples.append({
                "description": candidate["description"],
                "transaction_type": candidate["transaction_type"],
                "amount_pence": candidate["amount_pence"] or 0,
                "category": candidate["category"],
            })
            if len(examples) >= count:
                break
        selected.append(examples)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--model", choices=("general", "content-tagging", "pcc"), default="general")
    parser.add_argument("--test-fraction", type=float, default=0.20)
    parser.add_argument("--examples", type=int, default=3, help="include this many nearest labelled training examples per Apple prompt")
    parser.add_argument("--merchant-min-support", type=int, default=2, help="minimum training examples for cleaned-merchant lookup")
    parser.add_argument("--merchant-purity", type=float, default=0.95, help="minimum winning-label share for cleaned-merchant lookup")
    parser.add_argument("--limit", type=int, help="score only the first N unresolved test rows as a quick smoke run")
    parser.add_argument("--output", type=Path, default=OUTPUT_CSV, help=f"local review CSV (default: {OUTPUT_CSV})")
    args = parser.parse_args()
    if not 0.05 <= args.test_fraction <= 0.5:
        parser.error("--test-fraction must be between 0.05 and 0.5")
    if args.examples < 0:
        parser.error("--examples cannot be negative")
    if args.merchant_min_support < 1:
        parser.error("--merchant-min-support must be positive")
    if not 0.5 < args.merchant_purity <= 1.0:
        parser.error("--merchant-purity must be greater than 0.5 and at most 1.0")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")

    database = args.database.expanduser().resolve()
    if not database.is_file():
        print(f"Database not found: {database}", file=sys.stderr)
        return 2

    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT e.transaction_date, COALESCE(t.transaction_type, '') AS transaction_type,
                          e.description,
                          e.description_key, e.amount_pence, e.account_name_key,
                          e.label_origin, c.name AS category
                   FROM training_examples AS e
                   LEFT JOIN transactions AS t ON t.transaction_id=e.source_transaction_id
                   JOIN categories AS c ON c.category_id=e.category_id
                   WHERE TRIM(e.description)<>'' AND TRIM(e.source_category)<>''"""
            ).fetchall()

        if len(rows) < 20:
            raise ValueError(f"Only {len(rows)} labelled examples found; need at least 20")
        train_rows, test_rows, cutoff = chronological_split(rows, args.test_fraction)
        if not train_rows or not test_rows:
            raise ValueError("The chronological split produced an empty training or test set")

        manually_confirmed: dict[tuple[str, int, str, str], set[str]] = defaultdict(set)
        imported_lookup: dict[tuple[str, int, str, str], set[str]] = defaultdict(set)
        for row in train_rows:
            key = transaction_key(row)
            if key is not None:
                target = manually_confirmed if row["label_origin"] == "manual" else imported_lookup
                target[key].add(row["category"])

        effective_train_rows = [
            row for row in train_rows
            if row["label_origin"] == "manual" or transaction_key(row) not in manually_confirmed
        ]
        # A generative classifier cannot learn categories absent from its past
        # labelled data. Don't offer unsupported choices such as empty legacy labels.
        supported_categories = sorted({row["category"] for row in effective_train_rows})
        merchant_counts: dict[str, Counter[str]] = defaultdict(Counter)
        for row in effective_train_rows:
            merchant = clean_description(row["description"])
            if merchant:
                merchant_counts[merchant][row["category"]] += 1
        merchant_lookup: dict[str, str] = {}
        for merchant, counts in merchant_counts.items():
            category, support = counts.most_common(1)[0]
            if sum(counts.values()) >= args.merchant_min_support and support / sum(counts.values()) >= args.merchant_purity:
                merchant_lookup[merchant] = category
        unresolved = []
        exact_predictions: dict[int, str] = {}
        merchant_predictions: dict[int, str] = {}
        exact_ambiguous = 0
        for row in test_rows:
            key = transaction_key(row)
            labels = set()
            if key is not None:
                labels = manually_confirmed.get(key) or imported_lookup.get(key, set())
            if len(labels) == 1:
                exact_predictions[id(row)] = next(iter(labels))
            elif len(labels) > 1:
                exact_ambiguous += 1
            else:
                merchant_category = merchant_lookup.get(clean_description(row["description"]))
                if merchant_category:
                    merchant_predictions[id(row)] = merchant_category
                else:
                    unresolved.append(row)

        if args.limit:
            unresolved = unresolved[:args.limit]
        if not unresolved:
            raise ValueError("There are no unresolved transactions in this held-out date range")

        y_true = [row["category"] for row in unresolved]
        baseline = make_model()
        baseline.fit(
            [model_text(row["description"], row["amount_pence"] or 0) for row in effective_train_rows],
            [row["category"] for row in effective_train_rows],
            classifier__sample_weight=training_sample_weights(effective_train_rows),
        )
        baseline_predictions = baseline.predict(
            [model_text(row["description"], row["amount_pence"] or 0) for row in unresolved]
        ).tolist()

        examples_by_row = nearest_examples(effective_train_rows, unresolved, args.examples)
        requests = []
        for index, (row, examples) in enumerate(zip(unresolved, examples_by_row)):
            requests.append({
                "id": str(index),
                "description": row["description"],
                "amount_pence": row["amount_pence"] or 0,
                "transaction_type": row["transaction_type"],
                "categories": supported_categories,
                "examples": examples,
            })
        json_lines = "".join(json.dumps(request, ensure_ascii=False) + "\n" for request in requests)
        command = ["swift", "run", "--package-path", str(BRIDGE_PACKAGE), "apple-model-bridge", "--model", args.model]
        completed = subprocess.run(command, input=json_lines, text=True, capture_output=True)
        if completed.returncode != 0:
            if completed.stderr:
                print(completed.stderr.strip(), file=sys.stderr)
            raise RuntimeError("Apple model bridge did not complete; no evaluations were saved")

        results = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        if len(results) != len(unresolved):
            raise RuntimeError(f"Bridge returned {len(results)} rows for {len(unresolved)} requests")
        errors = [row for row in results if row.get("status") != "ok"]
        if errors:
            raise RuntimeError(f"Apple model failed on {len(errors)} row(s); first error: {errors[0].get('error', 'unknown error')}")
        apple_predictions = [row["category"] for row in results]
        scored_categories = sorted(set(y_true) | set(baseline_predictions) | set(apple_predictions))

        print("local-finance-tracker Apple Foundation Model evaluation")
        print("=" * 39)
        print(f"Model: {args.model}; date cutoff: {cutoff}")
        print(f"Training rows: {len(effective_train_rows)}; held-out rows: {len(test_rows)}")
        print(f"Exact lookup matches: {len(exact_predictions)}; consistent merchant matches: {len(merchant_predictions)}; ambiguous exact matches: {exact_ambiguous}")
        print(f"Apple/local classifier rows after lookup: {len(unresolved)}")
        print(f"Merchant rule: at least {args.merchant_min_support} training examples and {args.merchant_purity:.0%} label purity")
        print(f"Nearest labelled examples included per prompt: {args.examples}")
        write_report("Current TF-IDF + logistic classifier (same remaining rows)", y_true, baseline_predictions, scored_categories)
        write_report("Apple Foundation Model (same remaining rows)", y_true, apple_predictions, scored_categories)

        scored_ids = {id(row): index for index, row in enumerate(unresolved)}
        pipeline_truth = []
        pipeline_baseline = []
        pipeline_apple = []
        for row in test_rows:
            if id(row) in exact_predictions:
                pipeline_truth.append(row["category"])
                pipeline_baseline.append(exact_predictions[id(row)])
                pipeline_apple.append(exact_predictions[id(row)])
            elif id(row) in merchant_predictions:
                pipeline_truth.append(row["category"])
                pipeline_baseline.append(merchant_predictions[id(row)])
                pipeline_apple.append(merchant_predictions[id(row)])
            elif id(row) in scored_ids:
                index = scored_ids[id(row)]
                pipeline_truth.append(row["category"])
                pipeline_baseline.append(baseline_predictions[index])
                pipeline_apple.append(apple_predictions[index])
        pipeline_categories = sorted(set(pipeline_truth) | set(pipeline_baseline) | set(pipeline_apple))
        write_report("Full pipeline: lookups + current classifier", pipeline_truth, pipeline_baseline, pipeline_categories)
        write_report("Full pipeline: lookups + Apple model", pipeline_truth, pipeline_apple, pipeline_categories)

        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.expanduser().open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=[
                "date", "description", "transaction_type", "amount_pence", "actual_category",
                "exact_lookup_category", "merchant_lookup_category", "classifier_category",
                "apple_category", "apple_pipeline_category", "apple_latency_ms",
            ])
            writer.writeheader()
            unresolved_ids = {id(row): index for index, row in enumerate(unresolved)}
            results_by_id = {id(row): results[index] for index, row in enumerate(unresolved)}
            for row in test_rows:
                index = unresolved_ids.get(id(row))
                exact_category = exact_predictions.get(id(row), "")
                merchant_category = merchant_predictions.get(id(row), "")
                classifier_category = baseline_predictions[index] if index is not None else ""
                apple_category = apple_predictions[index] if index is not None else ""
                apple_pipeline_category = exact_category or merchant_category or apple_category
                apple_result = results_by_id.get(id(row), {})
                writer.writerow({
                    "date": row["transaction_date"],
                    "description": row["description"],
                    "transaction_type": row["transaction_type"],
                    "amount_pence": row["amount_pence"],
                    "actual_category": row["category"],
                    "exact_lookup_category": exact_category,
                    "merchant_lookup_category": merchant_category,
                    "classifier_category": classifier_category,
                    "apple_category": apple_category,
                    "apple_pipeline_category": apple_pipeline_category,
                    "apple_latency_ms": apple_result.get("elapsed_milliseconds", ""),
                })
        print(f"\nRow-by-row comparison saved locally to: {args.output.expanduser()}")
        return 0
    except (OSError, sqlite3.Error, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Apple model evaluation error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
