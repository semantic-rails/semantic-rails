-- Portable seed: DuckDB and Postgres run this same script, and comments here hold no semicolons.
-- The package declares no team key on members: their events name a team. Member 1 has
-- events in teams 10 and 20, member 2 two events in team 10, and member 4 none. The table's
-- own team_id (each member's home team) is read only by a test variant.
CREATE TABLE members (member_id INTEGER, member_name VARCHAR(16), team_id INTEGER);
INSERT INTO members VALUES (1, 'Ann', 20), (2, 'Bo', 10), (3, 'Cy', 30), (4, 'Di', 40),
  (5, 'Ed', 20);

CREATE TABLE teams (team_id INTEGER, plan_tier VARCHAR(8), created_at TIMESTAMP);
INSERT INTO teams VALUES
  (10, 'pro', TIMESTAMP '2024-01-10 10:00:00'),
  (20, 'free', TIMESTAMP '2024-01-20 10:00:00'),
  (30, 'pro', TIMESTAMP '2024-03-05 09:00:00'),
  (40, 'free', TIMESTAMP '2024-03-15 09:00:00');

CREATE TABLE member_events (
  event_id INTEGER, member_id INTEGER, team_id INTEGER, event_kind VARCHAR(8),
  occurred_at TIMESTAMP
);
INSERT INTO member_events VALUES
  (1, 1, 10, 'join', TIMESTAMP '2024-02-01 09:00:00'),
  (2, 1, 20, 'join', TIMESTAMP '2024-02-02 09:00:00'),
  (3, 2, 10, 'join', TIMESTAMP '2024-02-03 09:00:00'),
  (4, 2, 10, 'post', TIMESTAMP '2024-02-04 09:00:00'),
  (5, 3, 30, 'join', TIMESTAMP '2024-03-06 09:00:00'),
  (6, 5, 20, 'join', TIMESTAMP '2024-03-07 09:00:00');

CREATE TABLE plans (plan_id INTEGER, plan_name VARCHAR(16));
INSERT INTO plans VALUES (1, 'Builder'), (2, 'Pro');

-- Each team signs up when it is created. The January teams' first billing versions start
-- 5 ms after the signup, the March teams' exactly at it. Team 20 moves to Builder in March.
CREATE TABLE team_signups (signup_id INTEGER, team_id INTEGER, signed_up_at TIMESTAMP);
INSERT INTO team_signups VALUES
  (100, 10, TIMESTAMP '2024-01-10 10:00:00'),
  (200, 20, TIMESTAMP '2024-01-20 10:00:00'),
  (300, 30, TIMESTAMP '2024-03-05 09:00:00'),
  (400, 40, TIMESTAMP '2024-03-15 09:00:00');

CREATE TABLE team_billing_history (
  team_id INTEGER, valid_from TIMESTAMP, valid_to TIMESTAMP, plan_id INTEGER, seats INTEGER
);
INSERT INTO team_billing_history VALUES
  (10, TIMESTAMP '2024-01-10 10:00:00.005', NULL, 1, 3),
  (20, TIMESTAMP '2024-01-20 10:00:00.005', TIMESTAMP '2024-03-01 00:00:00', 2, 5),
  (20, TIMESTAMP '2024-03-01 00:00:00', NULL, 1, 2),
  (30, TIMESTAMP '2024-03-05 09:00:00', NULL, 1, 4),
  (40, TIMESTAMP '2024-03-15 09:00:00', NULL, 2, 6);
