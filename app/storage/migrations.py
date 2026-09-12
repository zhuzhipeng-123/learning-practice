"""Additive migrations preserve existing task and answer references."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from app.storage.transactions import transaction

CURRENT_VERSION = 7
MIGRATION_2 = [
    ("CREATE TABLE answer_exposure (attempt_id TEXT NOT NULL REFERENCES attempt(id),"
     "happened_at TEXT NOT NULL, PRIMARY KEY(attempt_id,happened_at))"),
    ("CREATE TABLE source_sync_state (source_id TEXT PRIMARY KEY REFERENCES source(id), "
     "snapshot_id TEXT NOT NULL REFERENCES source_snapshot(id))"),
    ("CREATE TABLE version_resources (version_id TEXT PRIMARY KEY REFERENCES question_version(id), "
     "reference_ids_json TEXT NOT NULL DEFAULT '[]', materials_json TEXT NOT NULL DEFAULT '[]')"),
    ("CREATE TABLE candidate_resolution (candidate_id TEXT PRIMARY KEY REFERENCES parser_candidate(id), "
     "question_id TEXT NOT NULL REFERENCES question(id))"),
    "DROP INDEX task_pending_question",
    ("CREATE UNIQUE INDEX task_pending_question ON task(plan_id,question_id,question_version_id) "
     "WHERE status IN ('pending','in_progress')"),
]
MIGRATION_3 = [
    ("CREATE TABLE question_exposure (question_id TEXT NOT NULL REFERENCES question(id), "
     "happened_at TEXT NOT NULL, PRIMARY KEY(question_id,happened_at))"),
    ("CREATE TABLE llm_module_settings (module TEXT PRIMARY KEY, provider TEXT NOT NULL, "
     "model TEXT NOT NULL, prompt TEXT NOT NULL, max_tokens INTEGER NOT NULL, updated_at TEXT NOT NULL)"),
    ("CREATE TABLE model_request (job_id TEXT PRIMARY KEY REFERENCES model_job(id), module TEXT NOT NULL, "
     "config_json TEXT NOT NULL, input_json TEXT NOT NULL, prompt_hash TEXT NOT NULL, response_text TEXT)"),
]
MIGRATION_4 = ["ALTER TABLE model_request ADD COLUMN response_model TEXT"]
MIGRATION_5 = [
    ("CREATE TABLE evaluation_job_target (job_id TEXT PRIMARY KEY REFERENCES model_job(id), "
     "attempt_id TEXT NOT NULL REFERENCES attempt(id))"),
    ("INSERT INTO evaluation_job_target SELECT j.id,a.id FROM model_job j JOIN attempt a "
     "ON j.business_key='evaluation:'||a.id WHERE j.purpose='theory_evaluation'"),
    "CREATE INDEX evaluation_job_attempt ON evaluation_job_target(attempt_id)",
    "CREATE TABLE provider_cooldown (provider TEXT PRIMARY KEY, retry_at TEXT NOT NULL)",
]
MIGRATION_6 = [
    ("CREATE TABLE source_tree_member(root_source_id TEXT NOT NULL REFERENCES source(id),node_token TEXT NOT NULL,"
     "source_id TEXT REFERENCES source(id),obj_type TEXT NOT NULL,title TEXT NOT NULL,path TEXT NOT NULL,"
     "status TEXT NOT NULL DEFAULT 'active',missing_count INTEGER NOT NULL DEFAULT 0,"
     "PRIMARY KEY(root_source_id,node_token))"),
    ("CREATE TABLE alignment_run(id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES source(id),"
     "started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,summary_json TEXT NOT NULL DEFAULT '{}',error TEXT)"),
]


MIGRATION_7 = ["CREATE TABLE interview_setup(session_id TEXT PRIMARY KEY REFERENCES interview_session(id), job_focus TEXT NOT NULL)"]


def migrate(connection, backup=True):
    version = connection.execute("SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
    if version > CURRENT_VERSION:
        raise RuntimeError("database schema is newer than this application")
    if version >= CURRENT_VERSION:
        return
    location = connection.execute("PRAGMA database_list").fetchone()[2]
    if location and backup:
        backup_path = Path(location).with_name(f"pre-migration-v{version}-{datetime.now(UTC):%Y%m%d%H%M%S%f}.db")
        with closing(sqlite3.connect(backup_path)) as destination:
            connection.backup(destination)
    with transaction(connection):
        for target, statements in ((2, MIGRATION_2), (3, MIGRATION_3), (4, MIGRATION_4), (5, MIGRATION_5), (6, MIGRATION_6), (7, MIGRATION_7)):
            if version < target:
                for statement in statements:
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_version VALUES (?,?)", (target, datetime.now(UTC).isoformat()))
