-- Link manually confirmed classifications to their source bank transaction.
ALTER TABLE training_examples
    ADD COLUMN source_transaction_id INTEGER REFERENCES transactions(transaction_id);

CREATE UNIQUE INDEX training_examples_source_transaction_idx
    ON training_examples(source_transaction_id)
    WHERE source_transaction_id IS NOT NULL;
