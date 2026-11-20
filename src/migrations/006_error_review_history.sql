CREATE TABLE error_review_decisions (
    decision_id INTEGER PRIMARY KEY,
    example_id INTEGER NOT NULL REFERENCES training_examples(example_id),
    previous_category_id INTEGER NOT NULL REFERENCES categories(category_id),
    chosen_category_id INTEGER NOT NULL REFERENCES categories(category_id),
    previous_label_origin TEXT NOT NULL,
    previous_original_category_id INTEGER REFERENCES categories(category_id),
    previous_reviewed_at TEXT,
    source_transaction_id INTEGER REFERENCES transactions(transaction_id),
    previous_transaction_category_id INTEGER REFERENCES categories(category_id),
    previous_transaction_category_source TEXT,
    previous_transaction_review_required INTEGER,
    decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    undone_at TEXT
);
CREATE INDEX error_review_decisions_recent_idx
    ON error_review_decisions(decided_at DESC);
CREATE INDEX error_review_decisions_example_idx
    ON error_review_decisions(example_id, decision_id DESC);
