-- Apply once to an existing local-finance-tracker database. This preserves existing transactions.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

ALTER TABLE transactions RENAME TO transactions_before_training_lookup;

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
    UNIQUE (import_id, source_row_number)
);

INSERT INTO transactions (
    transaction_id, account_id, import_id, source_row_number, transaction_date,
    transaction_type, raw_description, amount_pence, balance_pence,
    category_id, category_source, review_required
)
SELECT transaction_id, account_id, import_id, source_row_number, transaction_date,
       transaction_type, raw_description, amount_pence, balance_pence,
       category_id, category_source, review_required
FROM transactions_before_training_lookup;

DROP TABLE transactions_before_training_lookup;

CREATE INDEX transactions_date_idx ON transactions(transaction_date);
CREATE INDEX transactions_account_date_idx ON transactions(account_id, transaction_date);
CREATE INDEX transactions_category_idx ON transactions(category_id);

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
    UNIQUE (training_import_id, source_row_number)
);

CREATE INDEX training_examples_exact_lookup_idx
    ON training_examples(transaction_date, amount_pence, account_name_key, description_key);
CREATE INDEX training_examples_category_idx
    ON training_examples(category_id);

COMMIT;
PRAGMA foreign_keys = ON;
