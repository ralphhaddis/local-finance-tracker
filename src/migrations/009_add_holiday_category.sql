INSERT INTO categories (name, kind)
VALUES ('Holiday', 'spending')
ON CONFLICT(name) DO NOTHING;
