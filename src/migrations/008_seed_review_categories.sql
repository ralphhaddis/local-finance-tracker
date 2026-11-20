INSERT INTO categories (name, kind) VALUES
    ('Going out', 'spending'),
    ('Retail', 'spending'),
    ('Activities', 'spending'),
    ('Medical', 'spending'),
    ('Miscellaneous', 'spending')
ON CONFLICT(name) DO NOTHING;
