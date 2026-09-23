"""Additive migrations preserve existing task and answer references."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from app.storage.transactions import transaction

CURRENT_VERSION = 16
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

MIGRATION_8 = [
    ("CREATE TABLE question_derivation(question_id TEXT PRIMARY KEY REFERENCES question(id), "
     "base_version_id TEXT NOT NULL REFERENCES question_version(id), model_job_id TEXT NOT NULL REFERENCES model_job(id))"),
]

MIGRATION_9 = [
    ("CREATE TABLE interview_derivation(question_id TEXT PRIMARY KEY REFERENCES question(id), "
     "parent_question_id TEXT NOT NULL REFERENCES question(id), session_id TEXT NOT NULL REFERENCES interview_session(id), "
     "turn_id TEXT REFERENCES interview_turn(id), reference_verified INTEGER NOT NULL DEFAULT 0, "
     "UNIQUE(session_id,turn_id))"),
    "CREATE INDEX task_question_history ON task(question_id,created_at)",
    "CREATE INDEX attempt_activity_history ON attempt(activity_date,submitted_at)",
    "CREATE INDEX interview_turn_session ON interview_turn(session_id,created_at)",
]


MIGRATION_10 = [
    ("CREATE TABLE free_practice_batch(id TEXT PRIMARY KEY,request_key TEXT NOT NULL UNIQUE,"
     "question_type TEXT NOT NULL CHECK(question_type IN ('code','theory')),payload_json TEXT NOT NULL,"
     "plan_id TEXT NOT NULL REFERENCES daily_plan(id),status TEXT NOT NULL "
     "CHECK(status IN ('queued','running','complete','failed','interrupted')),result_json TEXT,error TEXT,"
     "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"),
    "CREATE UNIQUE INDEX free_batch_active_kind ON free_practice_batch(question_type) WHERE status IN ('queued','running')",
]


MIGRATION_11 = [
    ("CREATE TABLE reference_correction(version_id TEXT PRIMARY KEY REFERENCES question_version(id),"
     "content TEXT NOT NULL,sources_json TEXT NOT NULL,created_at TEXT NOT NULL)"),
]

MIGRATION_12 = [
    ("CREATE TABLE reference_verification(version_id TEXT PRIMARY KEY REFERENCES question_version(id),"
     "verified INTEGER NOT NULL CHECK(verified IN (0,1)),evidence TEXT NOT NULL,created_at TEXT NOT NULL)"),
    ("INSERT INTO reference_verification SELECT q.current_version_id,d.reference_verified,"
     "'Migrated explicit user verification',q.created_at FROM interview_derivation d "
     "JOIN question q ON q.id=d.question_id WHERE q.current_version_id IS NOT NULL"),
    ("CREATE TABLE reference_correction_history(id TEXT PRIMARY KEY,version_id TEXT NOT NULL REFERENCES question_version(id),"
     "content TEXT NOT NULL,sources_json TEXT NOT NULL,created_at TEXT NOT NULL)"),
    ("INSERT INTO reference_correction_history SELECT 'legacy:'||version_id,version_id,content,sources_json,created_at "
     "FROM reference_correction"),
]

MIGRATION_13 = [
    ("CREATE TABLE alignment_request(request_key TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,"
     "payload_json TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"),
    ("CREATE TABLE alignment_request_run(request_key TEXT NOT NULL REFERENCES alignment_request(request_key),"
     "source_id TEXT NOT NULL REFERENCES source(id),run_id TEXT NOT NULL REFERENCES alignment_run(id),"
     "reused INTEGER NOT NULL DEFAULT 0 CHECK(reused IN (0,1)),PRIMARY KEY(request_key,source_id))"),
    "CREATE INDEX alignment_request_run_id ON alignment_request_run(run_id)",
]

MIGRATION_14 = [
    ("CREATE TABLE mastery_assessment(id TEXT PRIMARY KEY,question_id TEXT NOT NULL REFERENCES question(id),"
     "review_basis_id TEXT NOT NULL REFERENCES review_basis(id),attempt_id TEXT REFERENCES attempt(id),"
     "level TEXT NOT NULL CHECK(level IN ('unknown','vague','partial')),request_key TEXT NOT NULL UNIQUE,"
     "created_at TEXT NOT NULL)"),
    "CREATE INDEX mastery_assessment_current ON mastery_assessment(question_id,review_basis_id,created_at)",
    "CREATE INDEX mastery_assessment_attempt ON mastery_assessment(attempt_id)",
]

MIGRATION_15 = [
    "ALTER TABLE daily_plan ADD COLUMN theory_scope_json TEXT NOT NULL DEFAULT '{}'",
]

MIGRATION_16 = [
    ("CREATE TABLE model_job_execution(job_id TEXT PRIMARY KEY REFERENCES model_job(id),"
     "execution_id TEXT NOT NULL,started_at TEXT NOT NULL,heartbeat_at TEXT NOT NULL,"
     "lease_expires_at TEXT NOT NULL,deadline_at TEXT NOT NULL)"),
]


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
        for target, statements in ((2, MIGRATION_2), (3, MIGRATION_3), (4, MIGRATION_4), (5, MIGRATION_5), (6, MIGRATION_6), (7, MIGRATION_7), (8, MIGRATION_8), (9, MIGRATION_9), (10, MIGRATION_10), (11, MIGRATION_11), (12, MIGRATION_12), (13, MIGRATION_13), (14, MIGRATION_14), (15, MIGRATION_15), (16, MIGRATION_16)):
            if version < target:
                for statement in statements:
                    connection.execute(statement)
                if target == 12:
                    _retract_unverified_model_scores(connection)
                connection.execute("INSERT INTO schema_version VALUES (?,?)", (target, datetime.now(UTC).isoformat()))


def _retract_unverified_model_scores(connection):
    """Retain audit rows but withdraw legacy automatic scores lacking verification."""
    rows = connection.execute("SELECT e.id,a.id AS attempt_id,a.review_round_id,a.activity_date FROM evaluation e "
        "JOIN attempt a ON a.id=e.attempt_id JOIN question_version v ON v.id=a.question_version_id "
        "JOIN question q ON q.id=v.question_id LEFT JOIN reference_verification r ON r.version_id=v.id "
        "WHERE q.source_kind='derived' AND COALESCE(r.verified,0)=0 AND e.corrected_by_user=0 "
        "AND e.adopted=1 AND e.verdict!='unable_to_assess'").fetchall()
    for row in rows:
        connection.execute('UPDATE evaluation SET adopted=0 WHERE id=?', (row['id'],))
        connection.execute("UPDATE reflection SET stale=1 WHERE activity_date=? AND author='model'", (row['activity_date'],))
        from app.services.review import record_valid_pass
        record_valid_pass(connection, row['attempt_id'], False)
