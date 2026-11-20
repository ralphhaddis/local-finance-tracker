-- Canonicalise the existing Eating out category as Going out.
BEGIN IMMEDIATE;

UPDATE transactions
SET category_id = (SELECT category_id FROM categories WHERE name = 'Going out')
WHERE category_id = (SELECT category_id FROM categories WHERE name = 'Eating out');

UPDATE training_examples
SET category_id = (SELECT category_id FROM categories WHERE name = 'Going out')
WHERE category_id = (SELECT category_id FROM categories WHERE name = 'Eating out');

DELETE FROM categories WHERE name = 'Eating out';

COMMIT;
