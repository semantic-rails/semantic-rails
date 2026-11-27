CREATE TABLE consultants (consultant_id VARCHAR, consultant_name VARCHAR);
INSERT INTO consultants VALUES ('kim', 'Kim'), ('pat', 'Pat'), ('lee', 'Lee');

CREATE TABLE time_entries (
    entry_id INTEGER, consultant_id VARCHAR, work_at TIMESTAMP, hours INTEGER
);
INSERT INTO time_entries VALUES
    (1, 'kim', '2026-05-10 09:00:00', 8),
    (2, 'kim', '2026-06-03 09:00:00', 8),
    (3, 'kim', '2026-06-10 09:00:00', 8),
    (4, 'kim', '2026-06-20 09:00:00', 8),
    (5, 'pat', '2026-03-15 09:00:00', 5),
    (6, 'pat', '2026-05-10 09:00:00', 8),
    (7, 'pat', '2026-08-20 09:00:00', 24),
    (8, 'lee', '2026-05-15 09:00:00', 6),
    (9, 'lee', '2026-06-01 00:00:00', 10),
    (10, 'lee', '2026-06-20 09:00:00', 6);

CREATE TABLE consultant_periods (
    consultant_id VARCHAR, period_start TIMESTAMP, period_end TIMESTAMP, period_label VARCHAR
);
INSERT INTO consultant_periods VALUES
    ('kim', '2026-04-01', '2026-05-01', 'Apr'),
    ('kim', '2026-05-01', '2026-06-01', 'May'),
    ('kim', '2026-06-01', '2026-07-01', 'Jun'),
    ('pat', '2026-04-01', '2026-05-01', 'Apr'),
    ('pat', '2026-05-01', '2026-06-01', 'May'),
    ('lee', '2026-05-01', '2026-06-01', 'May'),
    ('lee', '2026-06-01', '2026-07-01', 'Jun');
