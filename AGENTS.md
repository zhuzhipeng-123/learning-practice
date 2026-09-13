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
- Theory module quotas follow the source heading tree with one expandable root. Explicit new batch draws may retire unfinished daily tasks, including started tasks; never overwrite submitted answers or change their frozen versions. Synchronization alone cannot edit plans.
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

- Latest clarified entry rule (September 13): opening or refreshing Today, Free Practice, or the Autumn entry shows no previous question list, batch, or session. Only an explicit action in that page may show its result. Do not reveal old results from page-load/focus polling or stored browser state. An explicitly opened interview session still supports continuous conversation and safe response recovery. Remove the standalone History module and its links; old /history bookmarks redirect to Review. Keep code/theory review collections and their per-question records. Submitted learning facts and review progress are not deleted.

- Latest user rule (September 13): daily and free practice retain only the current explicitly drawn batch. Successful replacement retires earlier unfinished tasks, including started but unsubmitted work; expired daily/free tasks do not carry across days or appear in History. Keep review membership and submitted learning facts. Review has distinct code and theory modules. Failed generation does not publish partial tasks; exact retries recover the original batch without replacing a newer one. Explain local draws versus model generation and restored batches. Stale practice submissions must explain retirement in Chinese and guide the user back to the current batch.
- Active interview pages reconcile outstanding follow-ups through read-only status checks after navigation or lost responses. Completed replies resume automatically without another model call; failures require explicit retry. Startup releases orphan model jobs; expired follow-up leases must remain explicitly recoverable. Preserve newer drafts and reject writes based on stale conversation revisions.
- New code question quality checks require concrete example evidence, including a quoted input, independently computed result and consistency decision. Preserve legacy request contracts and do not describe model checks as guaranteed correctness.

- Autumn interview practice is a continuous conversation: active sessions show the user's saved answers and interviewer questions without exposing hidden references or feedback. Archived answer-bearing views still record exposure. Sending an answer may continue the interview, but model failure never loses or duplicates the saved answer. Bind new answers and follow-up requests to the observed conversation revision; retries recover the original result and stale new writes must be rejected.
- Interviewers adapt to the actual conversation: clarify uncertain claims, probe evidenced mistakes, ease difficulty when the learner is stuck, and explore related untested areas without repeatedly trapping them on one question. Bounded learning-history context is evidence of recorded practice only, never proof of knowledge or ignorance. Preserve custom prompts and frozen requests; enforce the conversational contract for new jobs. Bound long model contexts, disclose omissions, and retain complete dialogue locally.
- Interview drafts must be isolated between browser tabs. New feedback must cite exact user-authored evidence for each observation; interviewer explanations cannot be attributed to the learner. Corrections cite both earlier and later user statements. Mark unanswered questions separately, retain rejected model output, allow one bounded evidence repair, and preserve legacy frozen feedback contracts.

- Browser usability acceptance must verify the actual serving process, not only a separate test server. A running process whose application files changed must reject new operations with a clear restart-required response, before any learning mutation. Page errors must render a usable HTML recovery page; API errors retain structured JSON. Check each visible action family using isolated data and label external-service mocks separately.
- September 13 audit repair: migrated source bindings are historical only. Restoring a retired anchor requires an explicit identity decision; old documents cannot overwrite or remove a question owned by its new source. Content containers must preserve their descendants and parsing gaps must not be reported as complete references.
- Generated references use version-bound verification, distinct from model quality checks. Unverified model references cannot automatically determine assessments or valid review passes; explicit human assessment remains available. Visible review previews count as answer exposure for the derived question on confirmation.
- Review actions from a saved task use its frozen version. Conflicting active review bases require explicit choice. Changing an already collected question must create an auditable version, never silently ignore edits or overwrite old question/answer snapshots.
- Plan edits, personal reflections and knowledge edits persist request keys and payloads transactionally. Recovery replays the original result without reverting later edits; stale new edits must be distinguished from retries.
- Version-bound source corrections must be available in reference views and new evaluation snapshots. Retried jobs retain their original evidence; re-evaluation may use newer corrections. Daily reflection context retains each answer's actual question across midnight, and late stale reflections do not replace current ones.
- Export verification checks all complete media references in the frozen database, including historical versions and candidates, not only files that happen to exist in the media directory.
- Adversarial acceptance uses varied synthetic topics and payloads, including wrong question/answer pairs, edited references, stale requests and reordered source results. Runtime behavior must not depend on fixture titles, fixed technical answers or tests' expected output strings.
- Reference verification is immutable per version. Migration v12 withdraws only legacy adopted automatic assessments of unverified derived references, retaining evaluation/answer history and recomputing affected passes. Human assessments and independently verified versions remain intact.
- Evaluation distinguishes reference sufficiency from answer correctness. When a model declares the reference insufficient or contradictory, a scoring verdict must be rejected. An unsupported guess is not evidence that the supplied reference can establish the correct answer.
- Version editors and source-correction editors are explicit answer views. They must record exposure, reject stale new edits, and preserve original request results on replay. Model retries keep their frozen reference corrections in both generation and quality-check requests.

- Direction suggestions and model reflections may receive at most one format correction per attempt; retain the rejected output and correction instruction in the request audit. Reject internal identifiers and double-escaped line breaks in reflection prose. Reflections use primary answers, questions and adopted evaluations, not earlier model summaries. Mark unanswered interview questions explicitly; unverified generated references are not feedback scoring rubrics. Render model Markdown tables with safe DOM nodes, never HTML execution.

- Recover uncertain requests with their original key and payload, preserving newer drafts. Model errors must distinguish completed failures from in-flight requests and cooldowns; never discard a running request solely because its HTTP status is 409. Completed evaluations and code self-assessments must update the visible state and survive reload.

- Every model-generated practice question, interview opening, and follow-up must retain a corresponding reference answer. Keep answers hidden until an explicit reference view; generation and quality checks do not count as learning or answer exposure.
- Structured model modules request JSON transport mode. Distinguish malformed JSON from truncation; never silently repair substantive content or accept incomplete question/answer pairs.
- Before publishing generated question/answer pairs, run a separate bounded quality check for requested scope, question-answer alignment, factual soundness, examples, and code completeness. Treat source notes as evidence to examine, not infallible truth. Permit at most one quality-driven revision, then reject with a recoverable reason if still unsuitable. Persist generation and checking provenance; never claim a model check guarantees correctness.
- Audit quality with unrelated-but-keyword-matching answers, ambiguous examples, false source claims, missing answers, and genuine model calls on isolated data. Do not hardcode particular topics or blacklist words to pass the examples.
- Independently verified source corrections belong in a separate version-bound `reference_correction` record with cited sources. Supply them to generation and checking without rewriting Feishu snapshots or saved attempts. A source version change must not inherit an old correction automatically. Keep corrections in database exports and model request evidence.

- Free code and theory practice are independent panels with their own source, mode, description, count, status, and retry controls. An operation may only lock controls that share its business object; unrelated work remains available.
- Saving a personal reflection or daily plan updates only that region. Daily inputs may be unapplied while the saved task list stays visible; never clear another region's draft or request by reloading the whole page.
- Persist free-practice batches, their frozen request, and results in SQLite. Fresh entry must not restore results automatically. After an explicit click, poll only that accepted batch; uncertain requests remain explicitly recoverable with their original keys. Editing settings within the current page must not erase the visible current result.
- Free generation runs in a bounded background executor with separate database connections, no remote calls inside transactions, and at most one active batch per question type. Retrying a failed or interrupted batch reuses its original request and model snapshots, while that batch remains current on the same date; expired batches cannot be resumed.
- Frontend acceptance must exercise delayed simultaneous code/theory generation, navigation while pending, completed-result restoration, failure and response-loss retries, cross-tab deduplication, independent form edits, and narrow layouts. Inspect shared statuses and whole-page reloads in other workflows too.

- September 12 review repair: original free draws always use never-submitted originals; generated variants remain a separate explicit option. Daily base goals and extra practice remain separate.
- Browser API requests have a finite total deadline including response-body reading. A timeout is an unknown outcome, never proof that a mutation failed; retain the original request for safe recovery and release the relevant controls.
- A failed validation must allow editing and resubmission. Unknown network outcomes retain the same payload and request key. Local browser drafts are not learning facts.
- Reading a daily reflection is not answer exposure. Only explicit answer/reference/answer-bearing history views apply the 24-hour rule. Compare timestamps as absolute instants.
- Review entry reuses same-basis tasks. When another version is already pending, show the existing task and explain the basis conflict; never silently replace started work or create a duplicate pending task.
- Interview follow-ups can be previewed, edited, explicitly confirmed into review, and traced to their session and turn. Model-derived references remain visibly unverified.
- Acceptance includes ten simulated Shanghai learning days in isolated storage, source changes/failures, reflections, review, restored backup, and browser retry/draft checks. Never claim fixture model responses validate real model quality.
- Daily and free activity heatmaps keep their separate scopes. There is no standalone History module; per-question records remain accessible from Review.

- Continuous synchronization is core functionality, not a one-time import.
- Task completion, adopted assessment, and review membership are separate states.
- A code answer of "cannot solve" completes the task and automatically enters review.
- Theory questions enter review only after an explicit user action or instruction.
- Free practice accepts a topic description or uniformly samples eligible never-submitted originals; pending tasks are excluded. Models identify a candidate pool, then code randomly samples and persists tasks.
- Free practice may explicitly generate variants from eligible original text. Store variants as derived questions with frozen source-version and model-job provenance; never add them to the Feishu original pool. Keep references hidden until requested. Generation failures must not create partial tasks. Daily and free activity are counted separately, and free tasks never increase daily targets.
- The current daily batch persists until explicit replacement or day rollover. Synchronization cannot redraw them, increase their target, clear completion, or change an in-progress attempt's version.
- Preserve question identity and learning history across content changes. Important changes require a new review basis; old passes cannot silently validate it.
- Source removal never deletes learning facts. Network, permission, pagination, parsing, or identity ambiguity never proves deletion.
- A review round exits only after at least five cumulative valid passes and `last_valid_pass_at - first_valid_pass_at > 604800` seconds. Failures do not reset prior passes.
- One interview main question is one task. Follow-ups do not increase the target. Derived questions are isolated from Feishu deletion logic.
- Save an answer before model evaluation. Model failure leaves it pending and must not duplicate completion or valid passes.
- User and model reflections are separate. Model claims must cite actual records and must not invent unseen code defects.
- Place the heatmap and personal/model reflection at the beginning of the homepage; list code and theory practice separately. Model reflection covers successes, weak points and pending assessments with question/session context, without revealing answers.
- Interview entry accepts a direction or model-suggested directions, with optional job focus. Also offer one-click review deep dives and classic questions without requiring a large question-bank selector.
- Homepage practice lists and progress reflect saved base code/theory targets only; extra practice belongs on its own pages. Free practice has separate code/theory draws and shows each new batch only after an explicit draw. Old unfinished free tasks are retired after replacement or day rollover and do not appear in History. Changed daily inputs are unapplied until saved.
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
