#BrokenSite-Weekly (BSW) — Execution Handoff

**Owner:** Michael Colenso
**Repo:** `michaelcolenso/BrokenSite-Weekly` (Python)
**Audience:** Coding agent (Claude Code or equivalent) executing with limited independent judgment. Follow this document literally. When this document and your own judgment conflict, follow this document and flag the conflict in your session summary. When something is genuinely ambiguous, STOP and ask Michael — do not improvise.

---

## 0. Mission and one-paragraph context

BSW scans local business websites for objective technical breakage and packages the results as a weekly, per-metro lead list sold to web freelancers and agencies at $39/mo. The scanner and detection taxonomy are the product. Michael is NOT building a web agency and will NOT do automated outreach. The product is **data**, delivered weekly. Pilot metro: **Seattle, WA**. Validation gate: **5 paying subscribers in one metro** before any expansion.

## 1. Hard rules (never violate)

1. **No automated outreach.** Do not build, scaffold, or suggest cold-email senders, contact-form auto-submitters, LinkedIn bots, or SMS blasts. The system produces lists; a human decides what to do with them.
2. **No anti-bot evasion.** Do not implement CAPTCHA solving, header/fingerprint spoofing beyond a single honest User-Agent, proxy rotation for evasion, or scraping of sites that block automated access. If a site blocks the scanner, skip it and record `status: blocked`.
3. **Respect robots.txt** for every crawl. Cache robots.txt per domain for 24h.
4. **Rate limits:** max 1 request per domain per 10 seconds; max 4 concurrent domains globally; total scan budget 2 requests/page, 3 pages/site (home, contact page if discoverable, one internal link).
5. **User-Agent string:** `BSW-Scanner/1.0 (+https://brokensiteweekly.com/bot)` — honest, with a link to a bot info page (Task 4.3).
6. **PII discipline:** collect only business-public data (business name, public phone, public address, website URL). No personal emails scraped from pages. No data resale beyond the subscription product.
7. **Scope discipline:** anything not listed as P0 below is not built in v1. Add ideas to `PARKING-LOT.md`, do not build them.
8. **Secrets** live in GitHub Actions secrets and Wrangler secrets only. Never commit keys. `.env.example` documents names only.
9. **Do not delete or rewrite existing repo history.** Work on branch `v1-rebuild`, merge via PR.

## 2. Architecture (target state)

```
[GitHub Actions cron: Mondays 06:00 PT]
        │
        ▼
[Python scanner (this repo)]
  1. Load business list for metro (data/metros/seattle.csv)
  2. Scan each domain (checks in §4)
  3. Score + screenshot flagged sites
  4. Emit results.json + screenshots/
        │ POST (bearer token)
        ▼
[Cloudflare Worker (Hono) + D1]        [R2: screenshots]
  - /ingest  (auth: bearer)
  - /api/leads?metro=&week=  (auth: subscriber token)
  - /report/:token  (HTML weekly report view)
        │
        ▼
[Weekly email via Resend API] → subscribers with signed report link
```

Business list source (P0): Washington Secretary of State corporations registry export filtered to active Seattle-area businesses with a website URL, supplemented by a one-time Google Places API pull for the top 20 consumer verticals (dentist, plumber, roofer, HVAC, landscaper, restaurant, salon, auto repair, electrician, remodeler, painter, movers, chiropractor, vet, gym, florist, locksmith, cleaner, accountant, law office). Target list size: 3,000–8,000 domains.

## 3. Repo layout (create if missing)

```
/scanner/
  __init__.py
  checks/           # one module per check, common interface
  crawl.py          # fetch logic, robots, rate limiting
  score.py          # severity scoring
  screenshot.py     # Playwright, flagged sites only
  emit.py           # results.json writer + POST to ingest
/data/
  metros/seattle.csv    # columns: business_name,vertical,phone,address,domain
/worker/              # Hono app, wrangler.toml, D1 migrations
/tests/
/PARKING-LOT.md
/HANDOFF.md           # this file
```

## 4. Detection checks (P0 — exactly these eight)

Each check returns `{check_id, triggered: bool, evidence: str, severity: int}`. A site is a **lead** if ≥1 check triggers.

| # | check_id | Trigger condition | Severity |
|---|----------|-------------------|----------|
| 1 | `ssl_expired` | TLS cert expired or hostname mismatch on port 443 | 5 |
| 2 | `no_https` | No 443 listener, or 443 unreachable while 80 serves | 4 |
| 3 | `dead_form` | Contact form present whose `action` URL returns ≥400, or form posts to a missing endpoint (HEAD/GET check only — NEVER submit the form) | 5 |
| 4 | `broken_pages` | ≥2 of 3 crawled pages return 404/500, or homepage itself 404/500/timeout | 5 |
| 5 | `not_mobile` | No `<meta name="viewport">` on homepage | 3 |
| 6 | `stale_copyright` | Footer copyright year ≤ 2021 (regex `(?:©|&copy;|\(c\))\s*(\d{4})`, take max year found) | 2 |
| 7 | `broken_images` | ≥3 `<img>` on homepage whose `src` returns ≥400 (check max 10 images) | 3 |
| 8 | `dead_cms` | Generator meta / asset paths reveal WordPress <5.0, Joomla <3.9, Drupal <8, or Flash embeds | 4 |

**Scoring:** lead severity = max(triggered severities) with +1 if ≥3 checks trigger (cap 5). Tier A = 5, Tier B = 4, Tier C = ≤3.

**Screenshots:** Playwright, homepage only, 1280×800, JPEG q70, only for Tier A/B leads. Store to R2 as `{week}/{domain}.jpg`.

**Accuracy gate (blocking):** after first full Seattle scan, output `verification_sample.csv` with 50 random flagged sites. Michael manually verifies. If true-positive rate <90%, fix the failing check(s) and rescan before ANY Phase-2 work. Record the rate in `ACCURACY.md`.

## 5. Data contracts

**results.json (scanner → ingest):**
```json
{
  "metro": "seattle",
  "week": "2026-W31",
  "scanned": 6412,
  "leads": [{
    "domain": "example.com",
    "business_name": "Example Plumbing",
    "vertical": "plumber",
    "phone": "+12065551234",
    "address": "123 Main St, Seattle, WA",
    "checks": [{"check_id": "ssl_expired", "evidence": "expired 2025-11-02", "severity": 5}],
    "tier": "A",
    "screenshot_key": "2026-W31/example.com.jpg"
  }]
}
```

**D1 tables:** `leads` (cols mirror above, PK domain+week), `subscribers` (email, token, metro, status, created_at), `scan_runs` (week, metro, scanned, lead_count, tp_rate NULLABLE).

## 6. Execution phases — do them in order, each gated

### Phase 1 — Scanner + accuracy (gate: ≥90% TP)
- [ ] 1.1 Build `crawl.py` (robots, rate limits, UA per §1)
- [ ] 1.2 Implement the 8 checks with unit tests (fixture HTML per check, happy + negative case each)
- [ ] 1.3 Build Seattle business list per §2; dedupe by domain; drop domains on national-chain blocklist (build a 100-entry blocklist: franchises/chains whose sites are corporate-managed)
- [ ] 1.4 Full scan; emit results.json + verification_sample.csv
- [ ] 1.5 STOP. Wait for Michael's verification pass.

### Phase 2 — Delivery pipeline (gate: end-to-end dry run works)
- [ ] 2.1 Worker: `/ingest` (bearer), D1 migrations, R2 binding
- [ ] 2.2 `/report/:token` HTML report: leads grouped by tier, screenshot thumbnails, CSV download link
- [ ] 2.3 GitHub Actions workflow: Monday 06:00 PT, scan → POST → notify Michael on failure
- [ ] 2.4 Resend email template: subject `Seattle broken-site leads — week of {date}`, body = counts by tier + report link. Send ONLY to addresses in `subscribers` table.

### Phase 3 — Sell (Michael-led; agent supports only)
- [ ] 3.1 Landing page (single HTML on Workers): one metro, one price ($39/mo), Stripe Payment Link (Michael creates the link — ask for it), sample redacted report embedded
- [ ] 3.2 Bot info page at `/bot` explaining scanner purpose + opt-out email
- [ ] 3.3 Generate `trades-cut.csv` each week: leads where vertical ∈ {roofer, plumber, HVAC, electrician, remodeler, painter, GC} — Michael's personal outreach list. Do not email anyone from it.
- [ ] Gate: 5 paying subscribers OR 8 weeks elapsed.

### Phase 4 — Expand (only if gate passed)
- One new metro per 5 subscribers, priority: Portland, Tacoma, Spokane. New metro = new CSV + config entry only; no code changes should be required (design Phase 1 accordingly).

**Kill criteria (from Michael):** <3 subscribers and no traction after 8 weeks → archive product, keep scanner as a library for PainDex integration. Do not begin PainDex integration in v1.

## 7. Non-goals (do not build in v1)

Multi-metro dashboard; user accounts/login; Stripe API integration (Payment Link only); lead CRM features; historical trend charts; API for third parties; any AI-generated site audits or outreach copy; monitoring/uptime products. Log ideas to PARKING-LOT.md.

## 8. Definition of done (v1)

- [ ] Monday cron produces a Seattle report with zero manual steps
- [ ] Accuracy ≥90% documented in ACCURACY.md
- [ ] Subscriber receives email ≤07:30 PT Monday with working report link
- [ ] All secrets in Actions/Wrangler secrets; repo contains none
- [ ] README updated with run instructions; this handoff kept current
- [ ] Total infra cost ≤ $10/mo at pilot scale

## 9. Open questions for Michael (blocking marked ★)

1. ★ Confirm domain: brokensiteweekly.com or a subdomain of an existing property?
2. ★ Stripe Payment Link URL (needed for 3.1).
3. Resend account exists? If not, approve creating one (free tier suffices).
4. Google Places API budget approval (~$50 one-time for list build) or WA SoS-only for v1?
5. Preferred email for scanner opt-out requests?
