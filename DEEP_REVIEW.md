# Deep Review: BrokenSite-Weekly

## Context understood

BrokenSite-Weekly is a weekly, unattended lead-gen pipeline that:
1. scrapes businesses from Google Maps,
2. scores their website health,
3. stores/dedupes in SQLite,
4. delivers tiered CSV exports to subscribers,
5. runs optional outreach/audit/contact/warm-lead workflows.

The orchestration is linear and phase-based in `src/run_weekly.py`, which keeps the operational model straightforward for systemd-timer automation.

---

## What is already strong

- **Clear phase architecture**: scrape → deliver → outreach pipeline is easy to reason about and operate.
- **Isolation boundaries**: modules use `*_with_isolation` wrappers to avoid crashing the whole run on one failure.
- **Pragmatic SQLite schema**: enough normalized tables (`leads`, `exports`, `audits`, `contacts`, `outreach`, `engagement_events`) for a single-node weekly system.
- **Good scoring breadth**: combines transport-level, content-level, and heuristic signals.
- **Tier-aware lead distribution**: pro/basic gating and exclusive windows are a solid monetization primitive.

---

## Key optimization & improvement opportunities

### 1) Make outreach confidence threshold consistently configurable (implemented)

- **Why**: The app already exposes `OUTREACH_MIN_CONFIDENCE`, but outreach SQL had a hardcoded `0.7`, which can diverge from operator config.
- **Impact**: Better control of outreach quality vs volume, fewer surprises in production.
- **Status**: Implemented by threading `config.outreach.min_contact_confidence` into the DB query path and adding tests.

### 2) Throughput: parallelize website scoring with bounded workers

- **Current**: `run_scraping_phase()` processes businesses serially.
- **Opportunity**: Use a small thread pool (e.g., 4-10 workers) for `process_business` network-bound scoring.
- **Expected gain**: Significant runtime reduction per weekly run, especially across multi-city/category schedules.
- **Guardrails**: keep per-domain rate limits + circuit breaker to avoid aggressive traffic.

### 3) Database connection reuse in hot paths

- **Current**: many methods open/close a new SQLite connection via context manager.
- **Opportunity**: keep one write connection per run phase (or per thread for parallel scoring), batch operations where possible.
- **Expected gain**: lower overhead and lock contention if/when concurrency is added.

### 4) Strengthen observability with phase timings + per-signal counters

- Add structured metrics per phase:
  - phase duration
  - websites scored/minute
  - reason frequency histogram (`ssl_error`, `timeout`, `missing_viewport`, etc.)
  - subscriber delivery outcomes by tier
- This unlocks informed tuning of scoring weights and scrape targets.

### 5) Smart query rotation (ROI optimization)

- **Current**: fixed static city/category lists.
- **Opportunity**: rank combos by historic yield and score quality (e.g., weighted by leads above 60 score and conversion outcomes).
- **Result**: higher-value leads with same scrape budget.

### 6) Lead freshness policy improvements

- Add `last_scored_at` and optional `score_decay` so stale-but-once-bad sites can be rescored periodically.
- This helps avoid repeatedly surfacing leads that already fixed issues.

### 7) Outreach safety/compliance hardening

- Introduce domain-level send throttles and bounce reason taxonomy in suppression.
- Track per-template response/engagement to optimize copy while controlling sender reputation.

### 8) Data model and indexing tune-ups

- Consider indexes for frequent filters:
  - `leads(exported_pro_at, score)`
  - `leads(exported_basic_at, score)`
  - `contacts(confidence)` (if outreach filtering grows)
- Helps keep query latency predictable as dataset grows.

### 9) Backpressure + graceful shutdown checkpoints

- Shutdown handling exists, but adding explicit checkpoint commits and phase resumption markers can reduce rerun waste after interruptions.

### 10) Product features to increase ARPU

- **Subscriber portal analytics**: “new this week” diffs, category/city filters.
- **Lead enrichment add-ons**: domain age, tech stack snapshot, Lighthouse-lite diagnostics.
- **Priority packs**: curated “high-intent” cohorts (fast follow-up opportunities).

---

## Prioritized implementation roadmap

### Next 1-2 weeks (high ROI, low complexity)
1. ✅ Keep confidence threshold configurable end-to-end (done).
2. Add phase timers + reason histograms in logs/run stats.
3. Add top-yield city/category report from historical runs.

### Next 1-2 months (medium complexity)
4. Bounded parallel scoring with safe defaults.
5. Query rotation strategy based on yield quality.
6. Rescore policy for stale leads.

### Longer horizon
7. Portal analytics and subscriber self-serve filters.
8. Enrichment layer + conversion feedback loop to refine scoring.

---

## Risks to watch while optimizing

- Over-parallelization causing blocks/rate limiting.
- Scoring drift increasing false positives.
- Outreach volume changes harming sender health.

Use feature flags and dry-run comparisons before turning on major behavior changes.
