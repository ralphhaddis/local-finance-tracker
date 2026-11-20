PRAGMA foreign_keys = ON;

CREATE TABLE accounts (
    account_id      INTEGER PRIMARY KEY,
    account_number  TEXT NOT NULL UNIQUE,
    account_name    TEXT NOT NULL
);

CREATE TABLE categories (
    category_id     INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL
                    CHECK (kind IN ('spending', 'income', 'transfer'))
);

CREATE TABLE imports (
    import_id       INTEGER PRIMARY KEY,
    filename        TEXT NOT NULL,
    file_sha256     TEXT NOT NULL UNIQUE,
    imported_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    row_count       INTEGER NOT NULL CHECK (row_count >= 0)
);

CREATE TABLE transactions (
    transaction_id      INTEGER PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES accounts(account_id),
    import_id           INTEGER NOT NULL REFERENCES imports(import_id),
    source_row_number   INTEGER NOT NULL CHECK (source_row_number > 0),

    transaction_date    TEXT NOT NULL,
    transaction_type    TEXT NOT NULL,
    raw_description     TEXT NOT NULL,
    amount_pence        INTEGER NOT NULL,
    balance_pence       INTEGER,

    category_id         INTEGER REFERENCES categories(category_id),
    category_source     TEXT NOT NULL DEFAULT 'unreviewed'
                        CHECK (category_source IN
                               ('unreviewed', 'manual', 'rule', 'training_lookup', 'model')),
    review_required     INTEGER NOT NULL DEFAULT 1
                        CHECK (review_required IN (0, 1)),
    classification_confidence REAL
                        CHECK (classification_confidence IS NULL OR
                               (classification_confidence >= 0 AND classification_confidence <= 1)),
    classification_model TEXT,

    UNIQUE (import_id, source_row_number)
);

CREATE INDEX transactions_date_idx
    ON transactions(transaction_date);

CREATE INDEX transactions_account_date_idx
    ON transactions(account_id, transaction_date);

CREATE INDEX transactions_category_idx
    ON transactions(category_id);

CREATE TABLE training_imports (
    training_import_id INTEGER PRIMARY KEY,
    filename           TEXT NOT NULL,
    file_sha256        TEXT NOT NULL UNIQUE,
    imported_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    row_count          INTEGER NOT NULL CHECK (row_count >= 0)
);

CREATE TABLE training_examples (
    example_id          INTEGER PRIMARY KEY,
    training_import_id   INTEGER NOT NULL REFERENCES training_imports(training_import_id),
    source_row_number   INTEGER NOT NULL CHECK (source_row_number > 0),
    transaction_date    TEXT,
    description         TEXT NOT NULL,
    description_key     TEXT NOT NULL,
    amount_pence        INTEGER,
    account_name        TEXT,
    account_name_key    TEXT,
    source_category     TEXT NOT NULL,
    category_id         INTEGER NOT NULL REFERENCES categories(category_id),
    source_transaction_id INTEGER REFERENCES transactions(transaction_id),
    label_origin        TEXT NOT NULL DEFAULT 'imported'
                        CHECK (label_origin IN ('imported', 'manual')),
    original_category_id INTEGER REFERENCES categories(category_id),
    reviewed_at         TEXT,
    UNIQUE (training_import_id, source_row_number)
);

CREATE INDEX training_examples_exact_lookup_idx
    ON training_examples(transaction_date, amount_pence, account_name_key, description_key);
CREATE INDEX training_examples_category_idx
    ON training_examples(category_id);
CREATE UNIQUE INDEX training_examples_source_transaction_idx
    ON training_examples(source_transaction_id)
    WHERE source_transaction_id IS NOT NULL;

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
CREATE INDEX error_review_decisions_recent_idx ON error_review_decisions(decided_at DESC);
CREATE INDEX error_review_decisions_example_idx ON error_review_decisions(example_id, decision_id DESC);

INSERT INTO categories (name, kind) VALUES
    ('Uncategorised', 'spending'),
    ('Groceries', 'spending'),
    ('Utilities', 'spending'),
    ('Rent', 'spending'),
    ('Transport', 'spending'),
    ('Education', 'spending'),
    ('Going out', 'spending'),
    ('Retail', 'spending'),
    ('Activities', 'spending'),
    ('Medical', 'spending'),
    ('Holiday', 'spending'),
    ('Miscellaneous', 'spending'),
    ('Income', 'income'),
    ('Transfer', 'transfer');
