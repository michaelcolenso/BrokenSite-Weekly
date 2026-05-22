# BrokenSite Weekly Ship Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the weekly lead pipeline with a clean release boundary, backward-compatible database upgrades, authorized subscriber downloads, and an operator-verified deployment path.

**Architecture:** Keep the weekly systemd pipeline as the first release surface and treat tracking, portal, outreach, market reports, and competitor analysis as explicit enabled surfaces rather than accidental side effects. Harden SQLite migrations and portal access control before the production database or subscriber exports are exposed to the current code. Add release checks that cover local tests, old-database upgrade behavior, and VPS smoke runs without broadening the product scope before launch.

**Tech Stack:** Python 3.11+, pytest, SQLite, FastAPI, Playwright, systemd, Gumroad API, SMTP

---

## Release Decisions

- Ship the core weekly pipeline first: scrape, score, dedupe, tiered CSV export, Gumroad delivery, KPI output.
- Keep competitor analysis disabled by default until its scrape budget and CSV value are measured on a staging run.
- Decide explicitly whether launch includes outreach and the FastAPI tracking/portal service. If yes, deploy and verify the tracking service in the same release. If no, disable outreach and portal links in production configuration for the first weekly delivery.
- Do not ship from the current mixed worktree without separating source changes from `.env`, SQLite, logs, `__pycache__`, debug dumps, and output CSV/report artifacts.

## File Map

- `.gitignore`: keep credentials and runtime artifacts out of commits.
- `src/db.py`: make existing SQLite databases migrate to the current lead schema.
- `tests/test_db.py`: prove old databases gain new columns before write paths use them.
- `src/tracking.py`: authorize portal CSV downloads against the authenticated subscriber's exports.
- `tests/test_tracking.py`: prove subscriber A cannot download subscriber B's CSV by filename.
- `src/config.py` and `src/scoring.py`: expose and enforce a measured network-check budget for expensive scoring signals if staging data shows current defaults are too aggressive.
- `tests/test_scoring.py`: cover any scoring-budget behavior added before launch.
- `systemd/`: add a tracking service only if portal, audit pages, CTA tracking, or outreach tracking are in the launch scope.
- `README.md`, `SETUP.md`, and `RUNBOOK.md`: document the real launch surfaces, verification commands, and rollback checks.
- `.github/workflows/test.yml`: run the unit suite for release branches and pull requests.

### Task 1: Establish A Clean Release Boundary

**Files:**
- Create: `.gitignore`
- Review: `data/`, `logs/`, `output/`, `debug/`, `src/__pycache__/`, `tests/__pycache__/`

- [ ] **Step 1: Add the runtime artifact ignore rules**

```gitignore
.env
.env.*
!.env.example
venv/
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.coverage
htmlcov/
data/*.db
data/*.db-*
logs/
output/
debug/
```

- [ ] **Step 2: Identify already-tracked runtime artifacts before changing the index**

Run:

```bash
git ls-files data logs output debug src/__pycache__ tests/__pycache__
```

Expected: tracked database, log, output, and bytecode entries are listed in the current repo and must be removed from the release index without deleting the operator's local data.

- [ ] **Step 3: Stop tracking generated files while preserving local files**

Run:

```bash
git rm --cached -r data logs output debug src/__pycache__ tests/__pycache__
```

Expected: generated paths are staged as removals, but local operator artifacts still exist on disk.

- [ ] **Step 4: Inspect source-only release scope**

Run:

```bash
git status --short
git diff --stat
```

Expected: source, tests, docs, systemd units, and intentional templates are visible. `.env`, SQLite data, logs, reports, CSVs, and bytecode are not candidates for a release commit.

- [ ] **Step 5: Commit release hygiene**

```bash
git add .gitignore
git commit -m "chore: keep runtime artifacts out of releases"
```

### Task 2: Make Database Upgrades Backward Compatible

**Files:**
- Modify: `src/db.py`
- Modify: `tests/test_db.py`

- [ ] **Step 1: Write a failing migration test for an existing leads table**

Add a test that creates a database with the pre-competitor lead columns, initializes `Database`, writes a `Lead`, and verifies `competitors_json` exists:

```python
def test_existing_leads_table_adds_competitors_json(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE leads (
                place_id TEXT PRIMARY KEY,
                cid TEXT,
                name TEXT NOT NULL,
                website TEXT,
                address TEXT,
                phone TEXT,
                review_count INTEGER,
                city TEXT NOT NULL,
                category TEXT NOT NULL,
                score INTEGER NOT NULL,
                reasons TEXT,
                first_seen TIMESTAMP NOT NULL,
                last_seen TIMESTAMP NOT NULL,
                exported_count INTEGER DEFAULT 0,
                last_exported TIMESTAMP,
                exported_basic_at TIMESTAMP,
                exported_pro_at TIMESTAMP,
                exclusive_until TIMESTAMP,
                exclusive_tier TEXT,
                lead_tier TEXT
            )
            """
        )

    db = Database(DatabaseConfig(db_path=db_path))
    columns = {
        row["name"]
        for row in db._conn.execute("PRAGMA table_info(leads)").fetchall()
    }
    assert "competitors_json" in columns
```

- [ ] **Step 2: Run the migration test and confirm the old schema fails**

Run:

```bash
./venv/bin/python -m pytest tests/test_db.py::test_existing_leads_table_adds_competitors_json -v
```

Expected: FAIL because `_ensure_columns()` does not yet add `competitors_json` for an existing `leads` table.

- [ ] **Step 3: Add the missing migration**

In `Database._ensure_columns()` add:

```python
if "competitors_json" not in lead_columns:
    conn.execute("ALTER TABLE leads ADD COLUMN competitors_json TEXT")
```

- [ ] **Step 4: Prove the write path works after upgrade**

Extend the migration test with a `Lead(..., competitors_json='{"gap_text": "test"}')`, call `db.upsert_lead(lead)`, and assert the stored value can be selected back from `leads`.

- [ ] **Step 5: Run the database suite**

Run:

```bash
./venv/bin/python -m pytest tests/test_db.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit the migration**

```bash
git add src/db.py tests/test_db.py
git commit -m "fix: migrate competitor lead metadata"
```

### Task 3: Lock Portal Downloads To The Subscriber

**Files:**
- Modify: `src/tracking.py`
- Create or Modify: `tests/test_tracking.py`

- [ ] **Step 1: Write the failing authorization test**

Use a signed portal token for `alice@example.com`, create one export row for Alice and one for Bob, then request Bob's CSV with Alice's token:

```python
def test_portal_download_rejects_export_owned_by_other_subscriber(client, db, token_for_alice, tmp_path, monkeypatch):
    alice_csv = tmp_path / "alice.csv"
    bob_csv = tmp_path / "bob.csv"
    alice_csv.write_text("name\nalice\n", encoding="utf-8")
    bob_csv.write_text("name\nbob\n", encoding="utf-8")

    db.record_export("run-1", "alice@example.com", 1, str(alice_csv), tier="basic")
    db.record_export("run-1", "bob@example.com", 1, str(bob_csv), tier="basic")

    response = client.get(f"/portal/download/{bob_csv.name}?token={token_for_alice}")
    assert response.status_code == 404
```

- [ ] **Step 2: Run the focused tracking test**

Run:

```bash
./venv/bin/python -m pytest tests/test_tracking.py::test_portal_download_rejects_export_owned_by_other_subscriber -v
```

Expected: FAIL because token verification alone currently allows any existing CSV filename under `OUTPUT_DIR`.

- [ ] **Step 3: Resolve the requested export through subscriber history**

After `verify_portal_token()` returns `(email, expiry)`, restrict `filename` to the CSV basenames found in that subscriber's recent or allowed export rows before returning `FileResponse`.

```python
email, _ = verified
exports = _get_db().get_recent_exports(email, limit=50)
allowed_paths = {
    Path(item["csv_path"]).resolve()
    for item in exports
    if item.get("csv_path")
}
requested = (OUTPUT_DIR / filename).resolve()
if requested not in allowed_paths:
    return _render_error_page(
        title="File not found",
        message="The file you requested is not available for this account.",
        from_email=config.smtp.from_email,
        status_code=404,
    )
```

- [ ] **Step 4: Preserve the path traversal guard**

Keep the `OUTPUT_DIR` containment check before returning the file. Add a test that requests `../secret.csv` and expects rejection.

- [ ] **Step 5: Run portal and tracking tests**

Run:

```bash
./venv/bin/python -m pytest tests/test_portal_auth.py tests/test_tracking.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit the portal access fix**

```bash
git add src/tracking.py tests/test_tracking.py
git commit -m "fix: authorize portal export downloads"
```

### Task 4: Verify The Launch Scoring Budget

**Files:**
- Review: `src/config.py`
- Review: `src/scoring.py`
- Modify if needed: `src/config.py`, `src/scoring.py`, `tests/test_scoring.py`

- [ ] **Step 1: Benchmark a scrape-only staging run without email side effects**

Run:

```bash
./venv/bin/python -m src.run_weekly --validate --scrape-only --no-outreach
./venv/bin/python -m src.run_weekly --scrape-only --dry-run --no-outreach
```

Expected: logs show query success rate, phase duration, websites per minute, and top scoring reasons. No subscriber email is sent.

- [ ] **Step 2: Measure the expensive checks**

Inspect whether SSL expiry checks, image HEAD checks, social-link HEAD checks, Playwright fallbacks, and `SCRAPER_MAX_WORKERS` keep the dry run comfortably below the systemd two-hour timeout.

- [ ] **Step 3: If staging is too slow or noisy, add explicit feature toggles**

Use config names that make production intent obvious:

```python
ssl_expiry_check_enabled: bool = True
broken_image_check_enabled: bool = True
dead_social_check_enabled: bool = False
```

Gate the corresponding blocks in `evaluate_website()` and add unit tests proving disabled checks do not issue HEAD requests.

- [ ] **Step 4: Keep competitor analysis off for launch unless measured**

Verify production `.env` does not enable:

```bash
COMPETITOR_ANALYSIS_ENABLED=true
```

Expected: the first weekly launch does not re-scrape every qualifying lead's city/category cohort unless that cost has been accepted.

- [ ] **Step 5: Run scoring verification**

Run:

```bash
./venv/bin/python -m pytest tests/test_scoring.py tests/test_scoring_quickwins.py -v
```

Expected: PASS.

### Task 5: Make Deployment Match Launch Scope

**Files:**
- Modify: `README.md`
- Modify: `SETUP.md`
- Modify: `RUNBOOK.md`
- Create if tracking ships: `systemd/brokensite-tracking.service`
- Create: `.github/workflows/test.yml`

- [ ] **Step 1: Decide the production surfaces**

Record one of these choices in docs and `.env` examples:

```text
Core-only launch: OUTREACH_ENABLED=false and no portal URL is sent.
Full launch: tracking service deployed, TRACKING_BASE_URL reachable, PORTAL_SECRET configured, and outreach compliance fields configured.
```

- [ ] **Step 2: If full launch ships, add a FastAPI systemd service**

Use the same brokensite user and writable paths as the weekly job:

```ini
[Service]
User=brokensite
Group=brokensite
WorkingDirectory=/opt/brokensite-weekly
EnvironmentFile=/opt/brokensite-weekly/.env
ExecStart=/opt/brokensite-weekly/venv/bin/uvicorn src.tracking:app --host 127.0.0.1 --port 8000
Restart=on-failure
```

- [ ] **Step 3: Update operator docs**

Document:

- the actual weekly phases now present in `src/run_weekly.py`,
- how KPI snapshots and manual-review CSVs are produced,
- how portal/tracking is enabled or disabled,
- how to back up `data/leads.db` before upgrade,
- which dry-run command is required before the first scheduled delivery,
- rollback steps if the scheduled job fails after deployment.

- [ ] **Step 4: Add minimal CI**

Start with a workflow that installs dependencies, installs Playwright Chromium if tests require it, and runs:

```bash
python -m pytest
python -m src.run_weekly --validate --scrape-only --no-outreach
```

- [ ] **Step 5: Run release verification locally**

Run:

```bash
./venv/bin/python -m pytest
./venv/bin/python -m src.run_weekly --validate --scrape-only --no-outreach
```

Expected: PASS and `Configuration valid`.

- [ ] **Step 6: Commit docs and release automation**

```bash
git add README.md SETUP.md RUNBOOK.md systemd .github/workflows/test.yml
git commit -m "docs: define brokensite launch operations"
```

### Task 6: Deploy With A Rollback Point

**Files:**
- Operate on VPS: `/opt/brokensite-weekly/`

- [ ] **Step 1: Back up the production database and environment**

Run on the VPS:

```bash
sudo -u brokensite cp /opt/brokensite-weekly/data/leads.db /opt/brokensite-weekly/data/leads.db.pre-ship
sudo install -m 600 -o brokensite -g brokensite /opt/brokensite-weekly/.env /opt/brokensite-weekly/.env.pre-ship
```

- [ ] **Step 2: Pull the release and install dependencies**

Run:

```bash
cd /opt/brokensite-weekly
sudo -u brokensite git pull
sudo -u brokensite uv pip install -r requirements.txt --python /opt/brokensite-weekly/venv/bin/python
sudo -u brokensite /opt/brokensite-weekly/venv/bin/playwright install chromium
```

- [ ] **Step 3: Validate configuration for the chosen launch mode**

Core-only:

```bash
sudo -u brokensite /opt/brokensite-weekly/venv/bin/python -m src.run_weekly --validate --scrape-only --no-outreach
```

Full launch:

```bash
sudo -u brokensite /opt/brokensite-weekly/venv/bin/python -m src.run_weekly --validate
```

- [ ] **Step 4: Run a no-email production smoke**

Run:

```bash
sudo -u brokensite /opt/brokensite-weekly/venv/bin/python -m src.run_weekly --scrape-only --dry-run --no-outreach
sudo -u brokensite /opt/brokensite-weekly/venv/bin/python -m src.run_weekly --stats
```

Expected: scraper and scorer complete, stats are readable, and production SQLite opens after migrations.

- [ ] **Step 5: Start or reload systemd units**

Run:

```bash
sudo systemctl daemon-reload
sudo systemctl restart brokensite-weekly.timer
sudo systemctl status brokensite-weekly.timer
```

If tracking ships:

```bash
sudo systemctl enable --now brokensite-tracking.service
curl -fsS http://127.0.0.1:8000/health
```

- [ ] **Step 6: Observe the first scheduled run**

Verify:

```bash
sudo journalctl -u brokensite-weekly.service -f
sudo -u brokensite /opt/brokensite-weekly/venv/bin/python -m src.run_weekly --stats
```

Expected: one completed weekly run, expected lead export counts, expected email count, no migration or authorization errors, and retained rollback copies until the next successful cadence.
