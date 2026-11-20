"""Shared local classifier used by the watched import workflow."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline


MODEL_NAME = "tfidf-char-logistic-direction-c3-manual5-v3"
MANUAL_LABEL_WEIGHT = 5.0


class TrainingDataUnavailableError(ValueError):
    """Raised when there is not yet enough labelled data to train a model."""


def training_sample_weights(rows: Sequence[sqlite3.Row]) -> list[float]:
    """Give confirmed human labels more influence than imported source labels."""
    return [MANUAL_LABEL_WEIGHT if row["label_origin"] == "manual" else 1.0 for row in rows]


def model_text(description: str, amount_pence: int) -> str:
    direction = "inflow" if amount_pence > 0 else "outflow" if amount_pence < 0 else "zero_or_unknown"
    return f"{description} transaction_direction {direction}"


def make_model() -> Pipeline:
    return Pipeline([
        ("text", TfidfVectorizer(
            analyzer="char_wb", ngram_range=(2, 5), lowercase=True,
            strip_accents="unicode", min_df=1, sublinear_tf=True,
        )),
        # C=3 was consistently better than C=1 on the chronological evaluation
        # windows, while retaining class balancing for the small categories.
        ("classifier", LogisticRegression(C=3.0, max_iter=3000, class_weight="balanced", random_state=42)),
    ])


def train_from_database(connection: sqlite3.Connection) -> Pipeline:
    rows = connection.execute(
        """SELECT e.transaction_date, e.description, e.description_key,
                  e.amount_pence, e.account_name_key, e.label_origin,
                  c.name AS category
           FROM training_examples e JOIN categories c ON c.category_id=e.category_id
           WHERE TRIM(e.description)<>'' AND TRIM(e.source_category)<>''"""
    ).fetchall()
    if len(rows) < 20:
        raise TrainingDataUnavailableError(f"Only {len(rows)} labelled examples in the database; at least 20 are needed")
    manual_keys = {
        (r["transaction_date"], r["amount_pence"], r["account_name_key"], r["description_key"])
        for r in rows
        if r["label_origin"] == "manual" and r["transaction_date"] and r["amount_pence"] is not None and r["account_name_key"]
    }
    rows = [
        r for r in rows
        if r["label_origin"] == "manual" or r["transaction_date"] is None or r["amount_pence"] is None
        or not r["account_name_key"]
        or (r["transaction_date"], r["amount_pence"], r["account_name_key"], r["description_key"]) not in manual_keys
    ]
    categories = {r["category"] for r in rows}
    if len(categories) < 2:
        raise TrainingDataUnavailableError("At least two labelled categories are needed to train the classifier")
    model = make_model()
    model.fit(
        [model_text(r["description"], r["amount_pence"] or 0) for r in rows],
        [r["category"] for r in rows],
        classifier__sample_weight=training_sample_weights(rows),
    )
    return model


def predict(model: Pipeline, description: str, amount_pence: int) -> tuple[str, float]:
    return predict_many(model, [(description, amount_pence)])[0]


def predict_many(model: Pipeline, transactions: Sequence[tuple[str, int]]) -> list[tuple[str, float]]:
    """Classify a batch using one vectorization pass per bounded chunk."""
    vectorizer = model.named_steps["text"]
    classifier = model.named_steps["classifier"]
    results: list[tuple[str, float]] = []
    chunk_size = 256
    for start in range(0, len(transactions), chunk_size):
        chunk = transactions[start:start + chunk_size]
        texts = [model_text(description, amount) for description, amount in chunk]
        features = vectorizer.transform(texts)
        predictions = classifier.predict(features)
        probabilities = classifier.predict_proba(features).max(axis=1)
        results.extend((str(category), float(confidence)) for category, confidence in zip(predictions, probabilities))
    return results
