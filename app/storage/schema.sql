PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL UNIQUE,
    wiki_url TEXT NOT NULL,
    question_type TEXT NOT NULL CHECK (question_type IN ('code', 'theory')),
    identity TEXT NOT NULL DEFAULT 'bot',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_check_at TEXT,
    last_check_success_at TEXT,
    last_content_sync_at TEXT,
    last_complete_sync_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS source_dependency (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source(id),
    kind TEXT NOT NULL CHECK (kind IN ('media', 'sheet')),
    token TEXT NOT NULL,
    subresource_id TEXT,
    revision TEXT,
    content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    UNIQUE (source_id, kind, token, subresource_id)
);

CREATE TABLE IF NOT EXISTS sync_run (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    revision_before TEXT,
    revision_after TEXT,
    page_count INTEGER NOT NULL DEFAULT 0,
    block_count INTEGER NOT NULL DEFAULT 0,
    complete INTEGER NOT NULL DEFAULT 0 CHECK (complete IN (0, 1)),
    summary_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);

CREATE TABLE IF NOT EXISTS source_snapshot (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source(id),
    revision TEXT NOT NULL,
    dependency_versions_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    blocks_json TEXT NOT NULL,
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE (source_id, revision, dependency_versions_json, content_hash, parser_version)
);

CREATE TABLE IF NOT EXISTS category (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source(id),
    parent_id TEXT REFERENCES category(id),
    anchor_block_id TEXT NOT NULL,
    name TEXT NOT NULL,
    source_status TEXT NOT NULL DEFAULT 'active',
    UNIQUE (source_id, anchor_block_id)
);

CREATE TABLE IF NOT EXISTS review_basis (
    id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL,
    basis_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (question_id, basis_hash)
);

CREATE TABLE IF NOT EXISTS question (
    id TEXT PRIMARY KEY,
    question_type TEXT NOT NULL CHECK (question_type IN ('code', 'theory')),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('feishu', 'derived')),
    current_version_id TEXT,
    source_status TEXT NOT NULL DEFAULT 'active',
    first_submitted_at TEXT,
    is_classic INTEGER NOT NULL DEFAULT 0 CHECK (is_classic IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_binding (
    id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES question(id),
    source_id TEXT NOT NULL REFERENCES source(id),
    main_anchor_block_id TEXT NOT NULL,
    prompt_block_ids_json TEXT NOT NULL,
    reference_block_ids_json TEXT NOT NULL,
    anchor_aliases_json TEXT NOT NULL DEFAULT '[]',
    parser_version TEXT NOT NULL,
    confirmation_status TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    missing_observation_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_id, main_anchor_block_id)
);

CREATE TABLE IF NOT EXISTS question_version (
    id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES question(id),
    snapshot_id TEXT REFERENCES source_snapshot(id),
    review_basis_id TEXT NOT NULL REFERENCES review_basis(id),
    prompt TEXT NOT NULL,
    reference_text TEXT,
    category_path TEXT NOT NULL,
    material_status TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    change_summary TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (question_id, content_hash)
);

CREATE TABLE IF NOT EXISTS daily_plan (
    id TEXT PRIMARY KEY,
    plan_date TEXT NOT NULL UNIQUE,
    timezone TEXT NOT NULL,
    code_target INTEGER NOT NULL DEFAULT 0,
    theory_target INTEGER NOT NULL DEFAULT 0,
    added_target INTEGER NOT NULL DEFAULT 0,
    allocation_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES daily_plan(id),
    question_id TEXT NOT NULL REFERENCES question(id),
    question_version_id TEXT NOT NULL REFERENCES question_version(id),
    origin TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('base', 'added')),
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    cancelled_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS task_pending_question
ON task(plan_id, question_id) WHERE status IN ('pending', 'in_progress');

CREATE TABLE IF NOT EXISTS review_round (
    id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES question(id),
    review_basis_id TEXT NOT NULL REFERENCES review_basis(id),
    status TEXT NOT NULL DEFAULT 'active',
    entered_by TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_review_round
ON review_round(question_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS attempt (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES task(id),
    question_version_id TEXT NOT NULL REFERENCES question_version(id),
    review_round_id TEXT REFERENCES review_round(id),
    entry_mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    submitted_at TEXT,
    activity_date TEXT,
    answer_text TEXT,
    code_self_result TEXT CHECK (code_self_result IN ('can_solve', 'cannot_solve')),
    note TEXT,
    answer_exposed_at TEXT,
    request_key TEXT UNIQUE
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt_per_task
ON attempt(task_id) WHERE submitted_at IS NULL;

CREATE TABLE IF NOT EXISTS evaluation (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempt(id),
    status TEXT NOT NULL,
    verdict TEXT,
    raw_json TEXT,
    model_id TEXT,
    prompt_version TEXT,
    rubric_version TEXT,
    created_at TEXT NOT NULL,
    supersedes_id TEXT REFERENCES evaluation(id),
    adopted INTEGER NOT NULL DEFAULT 0 CHECK (adopted IN (0, 1)),
    corrected_by_user INTEGER NOT NULL DEFAULT 0 CHECK (corrected_by_user IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_adopted_evaluation
ON evaluation(attempt_id) WHERE adopted = 1;

CREATE TABLE IF NOT EXISTS valid_review_pass (
    id TEXT PRIMARY KEY,
    review_round_id TEXT NOT NULL REFERENCES review_round(id),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES attempt(id),
    activity_date TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    UNIQUE (review_round_id, activity_date)
);

CREATE TABLE IF NOT EXISTS review_event (
    id TEXT PRIMARY KEY,
    review_round_id TEXT NOT NULL REFERENCES review_round(id),
    event_type TEXT NOT NULL,
    happened_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS interview_session (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES task(id),
    question_version_id TEXT NOT NULL REFERENCES question_version(id),
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS interview_turn (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES interview_session(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reflection (
    id TEXT PRIMARY KEY,
    activity_date TEXT NOT NULL,
    author TEXT NOT NULL CHECK (author IN ('user', 'model')),
    content TEXT NOT NULL,
    version INTEGER NOT NULL,
    covered_ids_json TEXT NOT NULL DEFAULT '[]',
    stale INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE (activity_date, author, version)
);

CREATE TABLE IF NOT EXISTS model_job (
    id TEXT PRIMARY KEY,
    business_key TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    error TEXT,
    result_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parser_candidate (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source(id),
    snapshot_id TEXT NOT NULL REFERENCES source_snapshot(id),
    main_anchor_block_id TEXT NOT NULL,
    draft_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_at TEXT,
    UNIQUE (source_id, main_anchor_block_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS idempotency_record (
    request_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
