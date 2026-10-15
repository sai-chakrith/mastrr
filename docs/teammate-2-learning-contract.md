# Personalized learning: implementation and teammate handoff

This change implements the backend portion of Teammate 2's assignment. It does
not certify educational effectiveness. The question bank and reference notes
are unchanged. New tests use isolated databases, with explicitly synthetic items
where deterministic coverage is needed.

## What is implemented

- Topic-scoped, paragraph-level TF-IDF retrieval with cosine ranking. It uses
  the Python standard library so tutoring does not depend on native scientific
  libraries. Missing topic notes return no context; short notes are never padded
  with unrelated topics. An unscoped corpus search is still available internally.
- Learner evidence from diagnostic histories and learning-session answers.
  Actual selected answers are available for new learning sessions. Existing
  diagnostics only record correctness; the tutor explicitly reports this limit.
- Tutoring returns observed errors, a teaching strategy, sourced excerpts,
  an existing worked example when found, a follow-up question, and a suggested
  practice difficulty. It does not invent misconception classifications.
- Optional Claude generation receives bounded recent error context and trusted
  notes, without the learner identifier. Missing keys, missing notes, or provider
  failure use an explicitly labeled extractive fallback. Provider error payloads
  are not sent back to clients. This is prompt grounding, not a factual guarantee.
- Adaptive practice uses actual served-question difficulty. Each correct answer
  targets one level harder; each incorrect answer targets one level easier,
  clamped to 1–5, with nearest available difficulty used when necessary.
- Verification and delayed review use three fresh questions near difficulty 3.
  They provide no item-by-item correctness feedback before completion. They do
  not automatically write mastery estimates or resolve gaps.
- Freshness checks exclude diagnostic assignments, legacy retest assignments,
  learning exposures, and reserved learning items. Exact question-text duplicates
  (ignoring case and whitespace) are excluded too. This does not detect semantic
  paraphrases or establish psychometrically equivalent difficulty.
- Verification/review reserve all three questions at session creation. Practice
  chooses its next fresh question after each answer. There is at most one active
  learning session per learner/topic through these services. Restarting the same
  purpose resumes it. Switching purpose requires completion or abandonment.
- Start/answer/abandon operations serialize their database writes. Submitted
  question IDs must match the current item. Invalid options, out-of-order answers,
  duplicate answers, and completed-session replays are rejected.
- Coverage shortage returns an explicit status without recycling seen items.
  Mid-session shortage produces `passed: null`, never a successful check.
- Two successive unsuccessful learning sessions recommend prerequisite assessment
  or a simpler worked example. A prerequisite is not called weak merely because
  it is untested.
- Review dates persist in SQLite and can be queried on the learner's return.
  No external notification or background scheduler is added.

## Provisional policies

These are engineering defaults to be reviewed with the assessment owner, not
validated mastery or retention thresholds:

- Practice: 1–6 questions, default 3.
- Verification/review: exactly 3 questions; pass means at least 2 correct.
- A passing practice session recommends verification; it is not mastery evidence
  equivalent to a fresh verification check.
- An initial verification outcome schedules a check in 1 day. Failure also
  recommends immediate remediation; the next-day check does not imply success.
- Successful delayed reviews extend the interval to 3, 7, 14, then 30 days.
  Failure resets it to 1 day. Repeated immediate successful verifications do not
  postpone an already scheduled review.

## HTTP contract

The existing FastAPI app mounts the router at `/learning`. The only shared
`main.py` changes mount this router, initialize additive tables, and pass the
learner ID into legacy tutoring generation. Core assessment/database modules and
frontend files are not rewritten.

**Identity warning for integration:** `learner_id` is currently a caller-supplied
identifier, consistent with the existing app. Matching it against a session
prevents accidental cross-record access but is NOT authentication. Teammate 1/3
must derive this identity from the authenticated principal and enforce ownership
for every route before deployment.

### Personalized tutor

`POST /learning/tutor`

```json
{"learner_id":"student_042","sub_skill":"Percentages"}
```

Returns `explanation`, `generated_by` (`llm` or `extractive_fallback`),
`source_chunks`, `source_excerpts`, `coverage` (`available`, `limited`, `missing`),
`worked_example` (sourced passage or null), `diagnosis`, `learner_context`,
`teaching_strategy`, `practice_recommendation`, and `limitations`. Fallbacks
include `fallback_reason`. The explanation may be general when no observed
mistakes exist; this is labeled `diagnosis.status: insufficient_evidence`.

`coverage: available` means the requested number of topic paragraphs exists,
not that teaching is complete or semantically verified. An extractive fallback
is a reference excerpt, not a personalized generated explanation.

### Start or resume learning

`POST /learning/sessions`

```json
{"learner_id":"student_042","sub_skill":"Percentages","purpose":"practice","count":3}
```

Use `purpose: verification` for an immediate fresh check and `purpose: review`
for a due delayed check. `count` must be 3 for either check.

An active session returns `session_id`, `learner_id`, `sub_skill`, `purpose`,
`status: in_progress`, `question`, `question_number`, `target_count`, and
`result: null`. `question` includes ID, text, options, topic, and actual
difficulty; it never includes `correct_answer`.

A shortage returns `status: insufficient_coverage`, `session_id: null`,
`required_count`, `available_fresh_count`, and a message. Render this as a content
coverage limit; do not display it as learner failure or silently use old items.

`GET /learning/sessions/{session_id}?learner_id=student_042` restores a session.
`POST /learning/sessions/{session_id}/abandon` accepts `{"learner_id":"student_042"}`.
Abandoning does not erase exposure or reservations.

### Submit an answer

`POST /learning/sessions/{session_id}/answer`

```json
{"learner_id":"student_042","question_id":"the_current_assigned_id","selected_answer":"the_exact_option_text"}
```

Returns the session contract and `feedback`. Practice feedback includes
correctness and the expected answer to the completed item. Verification/review
feedback is null until the aggregate result is available.

On completion, `question` becomes null and `result` contains `n_correct`,
`n_total`, `accuracy`, `passed`, `purpose`, `next_action`,
`prerequisites_to_check`, `review`, `coverage`, `mastery_updated: false`, and
the provisional policy descriptor. Accuracy is the observed fraction, not
automatically 1.0 after passing. There is no confidence percentage.

`next_action` is one of `verify_with_fresh_questions`, `return_for_review`,
`review_explanation`, `assess_prerequisites`, `simpler_worked_example`, or
`request_content`. The frontend must route to that activity instead of always
starting another diagnostic.

### Delayed reviews

`GET /learning/reviews/{learner_id}` returns scheduled topics with `due_at`
(UTC ISO timestamp), `due`, `interval_days`, `successful_reviews`, and
`last_outcome`. Display times in the user's timezone. Starting a review before
its due time returns 409. A lack of fresh items leaves the review outstanding.

### Error responses

- 400: invalid service request or unknown topic.
- 404: session does not exist for the supplied learner.
- 409: stale answer, completed session, another active activity, or review not due.
- 422: malformed request fields.

## Storage and integration ownership

`backend/learning_store.py` owns an additive, idempotent migration introducing
`learning_sessions`, `learning_exposures`, and `learning_reviews`. It runs at
app startup and is safe to call on an existing database. It does not alter core
tables or delete learner progress. Teammate 1 can incorporate it into a future
central migration runner.

**Teammate 1:** Consume completed verification/review evidence through
`get_learning_session()` or the completed rows. Decide how it updates mastery and
gaps, with replay protection. Do not treat practice results as fresh verification.
Add selected-answer information to diagnostic history for richer tutoring.
Coordinate diagnostic and legacy retest selection with learning exposures and
reservations: the new service reads old assignments, but the old engines do not
yet read new reservations. Do not reseed question content during active sessions.

**Teammate 3:** Wire the `/learning` endpoints into the existing frontend. Add
practice, verification, review, resume, and repeated-failure actions; display
coverage limits and source excerpts. Derive identity from the account feature
before a real-user deployment.

The existing `/retest` and its mastery behavior remain Teammate 1's work. The
existing frontend still uses that legacy flow until Teammate 3 connects the new
routes. These changes do not claim that the full product is complete.

## Verification

Verified on 15 October 2026: **83 tests passed**, with two existing FastAPI
startup-event deprecation warnings. The HTTP test client required normal local
process access on this Windows host; the restricted process hung on it.

Run the existing and new tests with:

```powershell
.venv/Scripts/python.exe -m pytest -q
```

Tests cover real SQLite state transitions, fresh selection, exact-text duplicate
exclusion, grading validation, adaptive difficulty, concurrent writes, resume,
abandonment, evidence-based tutoring, provider fallback, review timing, and HTTP
integration. External model responses are stubbed only in model-branch tests.
These tests are not evaluations of live model correctness, misconception accuracy,
retention gains, or real learner outcomes; those require a separate pilot.
