# Classifier evaluation methodology & findings

## Method

- Data source: `training_examples` in the local SQLite database.
- Split: chronological 80/20 by transaction date, keeping all examples from each date together; undated rows are kept in training only.
- Pipeline: character-level TF-IDF (`char_wb`, 2–5 character n-grams, sublinear TF) and class-balanced logistic regression.
- Description variants compared: raw, locally cleaned (`src/description_cleaner.py`), and raw plus cleaned.
- Runtime: local scikit-learn 1.6.1. All transaction descriptions remain local to the host machine.
- Exact lookup and high-purity cleaned-merchant matching run before the classifier; conflicting exact keys are treated as review cases.

## Baseline & description cleaning findings

- On chronological holdout splits, exact transaction lookup alone produces zero cross-date matches (expected, since the lookup key includes `transaction_date`).
- **Raw vs. cleaned text:** Keeping the raw description consistently outperformed stripping card/location metadata into cleaned text alone (~83.2% accuracy / 0.593 macro F1 on raw vs. ~78.6% / 0.557 on cleaned-only in initial splits). The cleaner (`CLEANER_VERSION = "2"`, normalizing card date/type prefixes and `DDMMM GRS <reference>` payroll descriptions) is therefore used for **merchant grouping and lookup keys**, while the classifier retains raw text.
- **Confidence thresholds:** Uncalibrated logistic regression probabilities showed strong precision when thresholding high-confidence suggestions (~98.5%–99.0% precision on the top ~33%–43% of lookup misses in initial tests), though thresholds are not treated as calibrated guarantees and suggestions always remain in the review queue for confirmation.

## Amount-direction feature

- In training data, genuine `Income` rows are positive inflows while most spending categories are negative outflows—except card refunds, which are positive inflows belonging to spending categories.
- Appending an explicit `transaction_direction` token (`inflow` / `outflow` / `zero_or_unknown`) rather than hardcoding `positive = Income` improved both overall holdout accuracy (from 84.5% to 85.2%) and `Income` recall (from 90.0% to 95.0%) while allowing refunds to stay in the review set.
- `transaction_type` was evaluated and omitted from model features because legacy training imports lack a transaction-type column, which creates a train/test feature mismatch until enough native imports accumulate.

## Local classifier configuration & manual label weighting

- All live, CLI, and evaluation paths share one pipeline definition in `src/classifier_service.py`: `tfidf-char-logistic-direction-c3-manual5-v3`.
- Setting `C=3.0` with `class_weight="balanced"` improved macro F1 across rolling historical windows compared to `C=1.0`; disabling class balancing degraded macro F1 substantially (dropping to ~0.320 on holdout splits).
- Giving manually confirmed labels **5x sample weight** (`MANUAL_LABEL_WEIGHT = 5.0`) over imported historical labels improved chronological holdout accuracy and macro F1 (~86.9% / 0.723 → ~87.6% / 0.739, and up to +0.067 macro F1 on recent rolling windows) while preserving imported rows for coverage.

## Review grouping heuristics

- Pending model suggestions in the web UI group transactions that share predicted category and payment direction:
  - Exact cleaned-merchant groups always cluster together.
  - Cross-merchant clustering requires high business-name similarity (≥ 0.72 overall, or ≥ 0.55 when parsed trailing card location matches, using character sequence + distinctive-token overlap while ignoring common legal/outlet suffixes).
- On holdout evaluation, adding conservative similarity grouping slightly expanded group coverage while preserving ~97.6% pairwise label agreement (vs. ~98.4% for exact-merchant groups).

## Apple Foundation Models experiment (macOS)

- Evaluated `experiments/apple-foundation-model` on the same chronological holdouts using three nearest-neighbour labelled examples (retrieved via cosine similarity on cleaned descriptions) plus category definitions and signed amounts.
- Under identical lookup pipelines, the local TF-IDF + logistic regression classifier outperformed Apple's on-device general and content-tagging models on both overall accuracy (~71.8% vs. ~66.9% / ~64.3%) and lookup-miss accuracy (~65.5% vs. ~59.4%), with lower latency and zero external model dependency.
- Note: These figures are exploratory across historical splits; rare categories with sparse training examples require more confirmed reviews rather than hyperparameter tuning alone.

## Reproduce

From the project root, with the virtual environment active:

```sh
./.venv/bin/python ./src/evaluate_classifier.py
```
