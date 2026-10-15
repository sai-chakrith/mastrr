"""Additive storage owned by the learning service; core learner tables stay intact.

Teammate 1 can incorporate this idempotent migration in the shared migration
runner later. All operations use the existing configured SQLite database.
"""
from .database import get_cursor


LEARNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_sessions (
    session_id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL,
    sub_skill TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('practice', 'verification', 'review')),
    status TEXT NOT NULL DEFAULT 'in_progress',
    question_ids TEXT NOT NULL,
    answers TEXT NOT NULL DEFAULT '[]',
    target_count INTEGER NOT NULL,
    target_difficulty INTEGER NOT NULL,
    current_question_id TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS learning_sessions_learner_skill
    ON learning_sessions (learner_id, sub_skill);
CREATE TABLE IF NOT EXISTS learning_exposures (
    learner_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    source TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (learner_id, question_id)
);
CREATE TABLE IF NOT EXISTS learning_reviews (
    learner_id TEXT NOT NULL,
    sub_skill TEXT NOT NULL,
    due_at TEXT NOT NULL,
    interval_days INTEGER NOT NULL,
    successful_reviews INTEGER NOT NULL DEFAULT 0,
    last_outcome TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (learner_id, sub_skill)
);
"""


def init_learning_tables():
    with get_cursor(commit=True) as cur:
        cur.executescript(LEARNING_SCHEMA)
