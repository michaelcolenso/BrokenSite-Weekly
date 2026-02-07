# Implementation Plan

This plan is designed for another agent to execute. It is organized into phases with concrete steps, files to touch, and acceptance criteria.

## Phase 0 — Baseline & Safety (1 day)
**Goal:** Establish baseline behavior and safety checks before changes.

**Steps**
1. Create a tracking checklist (e.g., `PLAN_CHECKLIST.md`) to record:
   - Current runtime for weekly run.
   - Lead count and export count per run.
   - Known errors or warnings.
2. Run validation once (dry-run if available) to ensure the environment works.
3. Note any environment constraints (missing dependencies, API keys).

**Acceptance Criteria**
- Baseline metrics recorded.
- No new errors introduced.

---

## Phase 1 — Atomic DB Operations + Connection Reuse (2–3 days)
**Goal:** Remove dedupe race conditions and reduce DB overhead.

**Steps**
1. In `src/db.py`, centralize DB connection usage (single connection per `Database` instance or per run context).
2. Replace the check-then-insert pattern with a single atomic insert that:
   - Uses `INSERT ... ON CONFLICT` for `place_id`, and
   - Returns whether the row was newly inserted vs updated.
3. Update `run_weekly.py` to rely on the atomic insert outcome for dedupe decisions.

**Files**
- `src/db.py`
- `src/run_weekly.py`

**Acceptance Criteria**
- Duplicate inserts do not occur even under concurrent or repeated runs.
- Dedupe logic does not require a separate pre-check.

---

## Phase 2 — Scoring & Fetch Optimization (2–4 days)
**Goal:** Improve performance and reduce repeated network costs.

**Steps**
1. Introduce a shared `requests.Session` for scoring fetches in `src/scoring.py`.
2. Add a shared session for `contact_finder.py` and any other network modules that do repeated HTTP calls.
3. Update modules calling network requests to accept a session or use a module-level singleton.

**Files**
- `src/scoring.py`
- `src/contact_finder.py`

**Acceptance Criteria**
- Measurable reduction in total fetch time for a representative run.
- No behavior change to scoring logic.

---

## Phase 3 — Structured “reasons” Storage (2–4 days)
**Goal:** Improve data integrity and future analytics.

**Steps**
1. Convert `reasons` from comma-separated string to JSON array in DB.
2. Add a migration path:
   - Detect existing string values and convert on read or during migration.
3. Update downstream:
   - `audit_generator.py` reason parsing
   - `lead_utils.py` reason parsing
   - Any CSV export logic that expects string reasons

**Files**
- `src/db.py`
- `src/audit_generator.py`
- `src/lead_utils.py`
- `src/delivery.py`

**Acceptance Criteria**
- All reads and writes to `reasons` accept JSON arrays.
- No break in CSV exports or audit generation.

---

## Phase 4 — Lead Pipeline Concurrency (3–5 days)
**Goal:** Speed up weekly runs without overwhelming external services.

**Steps**
1. In `run_weekly.py`, batch the scraped businesses.
2. Score in parallel using a bounded thread pool (e.g., max 5–10 workers).
3. Add a retry budget (consider using `RetryBudget` from `retry.py`) to avoid runaway retries.

**Files**
- `src/run_weekly.py`
- `src/retry.py` (if `RetryBudget` needs enhancements)

**Acceptance Criteria**
- Same lead results vs baseline (within acceptable variance).
- Reduced total runtime.

---

## Phase 5 — Lead Quality Enhancements (2–4 days)
**Goal:** Add higher-value signals to improve ranking.

**Steps**
1. Verify that all scoring signals configured in `ScoringConfig` are applied consistently.
2. Add a “marketing intent” tag (e.g., `marketing_intent: true` if GTM/FB pixel present).
3. Add score stacking rules (bonus points when multiple neglect signals occur).

**Files**
- `src/scoring.py`
- `src/lead_utils.py`
- `src/config.py`

**Acceptance Criteria**
- Leads include new reasoning tags.
- Score adjustments are deterministic and logged.

---

## Phase 6 — “Unexpectedly Awesome” Features (5–10 days)
**Goal:** Differentiate product for subscribers.

### Feature A: Competitor Gap Report
**Steps**
1. For each lead, fetch top 3 competitors in the same city/category.
2. Score competitor sites and summarize “how far behind” the lead is.
3. Render in audit pages and CSV exports.

**Files**
- `src/maps_scraper.py`
- `src/scoring.py`
- `src/audit_generator.py`
- `src/delivery.py`

**Acceptance Criteria**
- Each lead includes competitor summary in audit output.

### Feature B: Weekly “Hot Leads” Digest
**Steps**
1. Add email summary of top 10 leads (highest scores).
2. Include “why now” reasons (e.g., SSL errors, downtime).

**Files**
- `src/delivery.py`
- `src/lead_utils.py`

**Acceptance Criteria**
- Subscribers receive summary and CSV attachment.

### Feature C: Warm Lead CTA Pipeline
**Steps**
1. Improve CTA form in `tracking.py` to capture availability and project size.
2. Notify pro subscribers with structured lead summaries.

**Files**
- `src/tracking.py`

**Acceptance Criteria**
- Submissions trigger email notifications with full context.

---

## Optional Testing & QA Plan
- Unit tests for scoring signals and lead tiering.
- Integration test for DB insert/dedupe.
- Manual run of `run_weekly.py --validate` for sanity checks.
