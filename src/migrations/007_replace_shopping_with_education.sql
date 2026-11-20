BEGIN IMMEDIATE;

INSERT INTO categories (name, kind)
VALUES ('Education', 'spending')
ON CONFLICT(name) DO NOTHING;

DELETE FROM categories WHERE name = 'Shopping';

COMMIT;
