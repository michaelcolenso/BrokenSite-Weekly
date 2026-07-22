# BrokenSite-Weekly (BSW) — Execution Handoff

**Owner:** Michael Colenso  
**Repo:** `michaelcolenso/BrokenSite-Weekly` (Python)  
**Audience:** Coding agent executing with limited independent judgment.

## Mission

BSW scans local business websites for objective technical breakage and packages the results as a weekly, per-metro lead list sold to web freelancers and agencies at $39/mo. The scanner and detection taxonomy are the product. Michael is not building a web agency and will not do automated outreach. The product is data, delivered weekly. Pilot metro: Seattle, WA. Validation gate: 5 paying subscribers in one metro before expansion.

## Hard rules

1. No automated outreach. Do not build cold-email senders, contact-form auto-submitters, LinkedIn bots, or SMS blasts.
2. No anti-bot evasion. Do not implement CAPTCHA solving, fingerprint spoofing beyond one honest User-Agent, or proxy rotation for evasion. If blocked, skip and record `status: blocked`.
3. Respect robots.txt for every crawl and cache robots.txt per domain for 24 hours.
4. Rate limits: max 1 request per domain per 10 seconds; max 4 concurrent domains globally; total scan budget 2 requests/page, 3 pages/site.
5. User-Agent: `BSW-Scanner/1.0 (+https://brokensiteweekly.com/bot)`.
6. Collect only business-public data: business name, public phone, public address, website URL. Do not scrape personal emails.
7. Scope discipline: anything outside P0 goes in `PARKING-LOT.md`.
8. Secrets live in GitHub Actions secrets and Wrangler secrets only. Never commit keys.
9. Do not delete or rewrite history. Work on branch `v1-rebuild`, merge by PR.

## P0 checks

Each check returns `{check_id, triggered: bool, evidence: str, severity: int}`. A site is a lead if at least one check triggers.

| check_id | Trigger | Severity |
| --- | --- | --- |
| `ssl_expired` | TLS certificate expired or hostname mismatch on port 443 | 5 |
| `no_https` | No 443 listener, or 443 unreachable while 80 serves | 4 |
| `dead_form` | Contact form action returns >=400 by HEAD/GET only; never submit | 5 |
| `broken_pages` | >=2 of 3 crawled pages return 404/500, or homepage itself fails | 5 |
| `not_mobile` | No viewport meta on homepage | 3 |
| `stale_copyright` | Footer copyright year <= 2021 using `(?:©|&copy;|\(c\))\s*(\d{4})`; take max year | 2 |
| `broken_images` | >=3 homepage images return >=400, checking max 10 images | 3 |
| `dead_cms` | WordPress <5.0, Joomla <3.9, Drupal <8, or Flash embeds | 4 |

Scoring: lead severity is the max triggered severity, plus 1 if at least 3 checks trigger, capped at 5. Tier A = 5, Tier B = 4, Tier C = <=3.

## Phase gates

1. Scanner + accuracy: build scanner, run Seattle scan, emit `results.json` and `verification_sample.csv`, then stop for Michael's manual verification. If true-positive rate is below 90%, fix checks and rescan before Phase 2.
2. Delivery pipeline: Worker ingest/report, D1/R2, GitHub Actions, Resend email only to subscribers.
3. Sell: one landing page, `/bot`, and `trades-cut.csv`; no automated outreach.
4. Expand: only after gate; one new metro per 5 subscribers.

## Open questions for Michael

1. Confirm domain: `brokensiteweekly.com` or subdomain?
2. Stripe Payment Link URL.
3. Resend account status.
4. Google Places API budget approval or WA SoS-only for v1?
5. Preferred scanner opt-out email.
