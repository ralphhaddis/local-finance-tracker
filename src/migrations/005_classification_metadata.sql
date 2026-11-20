-- Keep the classifier suggestion and its probability beside each imported transaction.
ALTER TABLE transactions ADD COLUMN classification_confidence REAL
    CHECK (classification_confidence IS NULL OR (classification_confidence >= 0 AND classification_confidence <= 1));
ALTER TABLE transactions ADD COLUMN classification_model TEXT;
