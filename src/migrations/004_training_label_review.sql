-- Keep manual review status and the imported label for auditability.
ALTER TABLE training_examples
    ADD COLUMN label_origin TEXT NOT NULL DEFAULT 'imported'
    CHECK (label_origin IN ('imported', 'manual'));

ALTER TABLE training_examples
    ADD COLUMN original_category_id INTEGER REFERENCES categories(category_id);

ALTER TABLE training_examples ADD COLUMN reviewed_at TEXT;

UPDATE training_examples
SET label_origin = 'manual', reviewed_at = CURRENT_TIMESTAMP
WHERE source_transaction_id IS NOT NULL;
