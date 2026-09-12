# Learning Practice Project Instructions

## Summary

Build a local personal learning, practice, and review web app. Feishu owns original questions, source references, and source categories. This app owns learning facts and derived interview questions.

The authoritative design is `docs/original-learning-app-plan.md`. Do not treat that design as already implemented or tested.

## Stack and structure

- Python 3.12
- FastAPI with Jinja2 templates
- Small amounts of plain JavaScript
- SQLite with versioned migrations and foreign keys enabled
- `app/adapters/`: external process and HTTP boundaries
- `app/parsers/`: Feishu block parsing and confirmed bindings
- `app/services/`: business transactions
- `app/storage/`: schema, migrations, and database access
- `tests/fixtures/`: small sanitized source and failure fixtures

## Repair and verification conventions

- `LEARNING_DATA_DIR` selects the local data directory; tests must always use a temporary directory.
- Load the project's ignored `.env` without overriding explicit environment variables.
- Keep fixtures out of live synchronization. Confirmed bindings describe boundaries, not frozen text.
- Use the current-version pointer, never wall-clock ordering, to determine current question content.
- Existing databases must migrate transactionally, with a backup before schema changes.
- Remote services are replaced by local fakes in automated tests. Never use real learning data.
- Browser and Computer Use verification are authorized for this repair; combine UI checks with isolated service/API tests.
- Daily and free allocation use the published local pool immediately. Source alignment runs only after an explicit alignment button action, in a deduplicated background job. Never hold a database transaction during remote calls.
- Wiki sources are recursive roots, not single documents. Enumerate every descendant with explicit pagination completion; retain each document's full Wiki path. A failed/partial tree traversal cannot prove document removal. Report document discovery, unsupported types, failures and model analysis separately.
- Theory module quotas follow the source heading tree with one expandable root. Explicit user edits may change today's targets and redistribute unstarted tasks; never cancel started/completed tasks, overwrite answers, or change their frozen versions. Synchronization alone cannot edit plans.
- Heatmap intensity counts distinct submitted main tasks per Shanghai date; viewing pages and model-only turns do not count. Day details group the day's latest task answers by code self-assessment or adopted theory assessment, including pending and unknown, and link to protected answer/dialogue views.
- Use Agnes as the primary model for all modules; OpenRouter Nex is an explicitly selected backup. Preserve independent editable prompts and existing request snapshots.
- Repair work is not complete until its regression checks pass; keep README and status reports factual.
- Model evaluation, interview follow-up, interview feedback, daily reflection, source boundary analysis, and topic selection have separate editable prompts and model selections. Keep provider credentials in ignored local configuration, never SQLite or exports.
- Credential names are exactly AGNES_API_KEY and OPENROUTER_API_KEY; do not introduce a shared ambiguous API_KEY alias.
- Support Agnes and OpenRouter through Chat Completions. Provider keys are isolated; switching provider must never reuse another provider's key. Never silently fall back to a different model or provider.
- Persist the exact prompt and model configuration used for a model job so later edits do not rewrite its audit history.
- Re-evaluation creates a new job for the same saved attempt with the current settings; retries retain their snapshot. Preserve human adoption and never duplicate learning completion.
- Persist provider cooldowns after HTTP 429 across modules in the same database. Honor Retry-After and require explicit retry after the wait; never switch providers automatically.

## Fixed business rules

- Continuous synchronization is core functionality, not a one-time import.
- Task completion, adopted assessment, and review membership are separate states.
- A code answer of "cannot solve" completes the task and automatically enters review.
- Theory questions enter review only after an explicit user action or instruction.
- Free practice accepts the user's own topic description or samples uniformly from all eligible originals when random is selected, including previously practiced originals; pending tasks are excluded. A separate optional new-only filter is allowed. Model topic matching may select only supplied question IDs and cannot invent fallback questions.
- Free practice may explicitly generate variants from eligible original text. Store variants as derived questions with frozen source-version and model-job provenance; never add them to the Feishu original pool. Keep references hidden until requested. Generation failures must not create partial tasks. Daily and free activity are counted separately, and free tasks never increase daily targets.
- Daily tasks persist. Synchronization cannot redraw them, increase their target, clear completion, or change an in-progress attempt's version.
- Preserve question identity and learning history across content changes. Important changes require a new review basis; old passes cannot silently validate it.
- Source removal never deletes learning facts. Network, permission, pagination, parsing, or identity ambiguity never proves deletion.
- A review round exits only after at least five cumulative valid passes and `last_valid_pass_at - first_valid_pass_at > 604800` seconds. Failures do not reset prior passes.
- One interview main question is one task. Follow-ups do not increase the target. Derived questions are isolated from Feishu deletion logic.
- Save an answer before model evaluation. Model failure leaves it pending and must not duplicate completion or valid passes.
- User and model reflections are separate. Model claims must cite actual records and must not invent unseen code defects.
- Place the heatmap and personal/model reflection at the beginning of the homepage; list code and theory practice in separate sections below the saved plan. Model reflection focuses on actual wrong answers and adopted evaluations, with question context and actionable follow-up.
- Interview entry accepts a user-written direction or model-suggested random directions, with optional job focus. Generate an opening question for the chosen direction; do not offer a question-bank selector.
- Homepage practice lists and progress reflect saved base code/theory targets only; extra practice belongs on its own pages. Free practice has separate code/theory draws and shows each new batch only after an explicit draw. Older free tasks are accessible in collapsed history. Changed daily inputs are unapplied until saved.
- Theory question anchors must be H1, H2, or H3 headings. H4-H6 and ordinary paragraphs are reference content, never standalone questions. Empty container headings remain modules. Exclude legacy non-heading questions from future allocation without deleting tasks, versions, or answers.
- Alignment accepts an optional user change description, retains it in the run report, and supplies it to model review. The description never proves deletion or replaces complete source discovery.
- Default screens show learning outcomes, not API JSON, internal IDs, or provider configuration. Keep independent editable prompts in collapsed sections with purpose and output-budget rationale. Rollover refreshes clean pages on local-day change; dirty forms must retain text.

## Current defaults, not immutable user requirements

- Bind only to `127.0.0.1` and use `Asia/Shanghai`.
- Pure notes and ambiguous compound sections become review candidates; explicit questions can publish automatically.
- Answer exposure blocks valid passes for 24 hours.
- Independent model jobs may run concurrently without a fixed interval. Each job retains its own lease to prevent duplicate execution; provider cooldowns apply only after a real HTTP 429.

Keep these defaults easy to review. Do not describe them as confirmed hard constraints.

## External access and safety

- Public Git history must not include personal Feishu identifiers or machine paths. Optional initial source settings live only in ignored `local-config.sources.json`; fresh installations register their own sources. Fixtures use synthetic identifiers.
- Never import or execute `/path/to/GenericAgent/mykey.py`.
- Never modify GenericAgent files.
- Never print, log, commit, export, or send secrets or authorization headers.
- Runtime Agnes configuration must come from environment variables or ignored local configuration.
- Invoke `lark-cli` with a fixed executable, an argv list, `shell=False`, a timeout, and an output-size limit.
- Treat Feishu content, user answers, and model output as untrusted data.
- Do not edit real learning documents for tests. Use local fixtures or a separately authorized test source.

## Code style

- Prefer explicit, beginner-readable Python over compressed or implicit constructs.
- Keep functions at 40 lines or fewer by default and separate validation, transformation, I/O, and presentation.
- Keep files around 150 lines where practical.
- Add comments for non-obvious intent, data shape, side effects, and edge cases.
- Do not add abstractions or features before a current phase requires them.

## Commands

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Delivery order and verification

Implement P0 through P6 in order as defined by the design. P1–P3 must form a reliable vertical slice before P4–P6. Every phase report must include actual checks, failures, remaining limitations, and a Chinese commit-message draft.

Do not claim a phase complete when required permissions or acceptance checks remain blocked. Do not run `git add`, `git commit`, or `git push` without explicit user confirmation.
