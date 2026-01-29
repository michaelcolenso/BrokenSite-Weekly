# BrokenSite-Weekly Improvement Roadmap

> Code review and enhancement plan - January 2026

This document outlines improvements identified during a comprehensive code review, plus feature enhancements to increase lead quality and subscriber value.

---

## Table of Contents

1. [Code Quality & Bug Fixes](#1-code-quality--bug-fixes)
2. [Scoring System Improvements](#2-scoring-system-improvements)
3. [Data Enrichment](#3-data-enrichment)
4. [Market Intelligence](#4-market-intelligence)
5. [Product Enhancements](#5-product-enhancements)
6. [Implementation Priority](#6-implementation-priority)

---

## 1. Code Quality & Bug Fixes

### High Priority

| Issue | Location | Description |
|-------|----------|-------------|
| Unused variable | `run_weekly.py:90` | `is_new` return value captured but never used |
| Race condition | `db.py:120-145` | `is_duplicate()` and `upsert_lead()` not atomic |
| Resource leak | `maps_scraper.py:352-457` | Playwright not using context manager pattern |
| CSV injection | `delivery.py:60-67` | User data written to CSV without sanitization |

### Medium Priority

| Issue | Location | Description |
|-------|----------|-------------|
| Import in function | `scoring.py:288` | `import time` should be at module level |
| Hardcoded weight | `scoring.py:331` | 4xx error weight not configurable |
| Redundant queries | `config.py:113-124` | "near me" redundant when city specified |
| Connection churn | `db.py:103-118` | New SQLite connection per operation |
| String storage | `db.py:29` | Reasons stored as comma-separated, not JSON |
| Dead code | `retry.py:116-134` | `RetryBudget` class never used |
| Double init | `run_weekly.py:245,333` | `setup_logging()` called twice |

### Lower Priority

| Issue | Location | Description |
|-------|----------|-------------|
| Missing rate limiting | `maps_scraper.py`, `gumroad.py` | No formal rate limiting implementation |
| No dry-run mode | `run_weekly.py` | Can't test without executing |
| No unit tests | - | Scoring logic should have test coverage |

---

## 2. Scoring System Improvements

### Currently Detected

| Signal | Weight | Status |
|--------|--------|--------|
| Unreachable | 100 | Implemented |
| Timeout | 90 | Implemented |
| 5xx errors | 85 | Implemented |
| SSL errors | 80 | Implemented |
| Parked domain | 75 | Implemented |
| No HTTPS | 30 | Implemented |
| Outdated copyright | 25 | Implemented |
| No viewport | 20 | Implemented |
| Flash/frames | 40 | Implemented |
| DIY builders | 5-10 | Implemented |

### Proposed Additions (Low Effort)

| Signal | Weight | Implementation |
|--------|--------|----------------|
| **Slow response** (>5s) | 20 | Already have `response_time_ms`, just need scoring |
| **Redirect chains** (>=3) | 15 | Check `len(response.history)` |
| **Last-Modified age** | 20 | Parse header, score if >2 years old |
| **Empty/generic title** | 10 | Regex check in `<title>` |
| **Missing meta description** | 10 | Check for `name="description"` |
| **Missing H1** | 5 | Simple HTML check |
| **Under construction** | 70 | Expand parked domain patterns |

### Proposed Additions (Medium Effort)

| Signal | Weight | Implementation |
|--------|--------|----------------|
| **SSL expiring** (<30d) | 25 | Socket SSL certificate check |
| **Broken images** | 15/each | HEAD request on sampled `<img src>` |
| **Missing phone/email** | 10/each | Regex patterns in HTML |
| **Dead social links** | 15 | Check Facebook/Instagram URLs |
| **Old developer credits** | 15 | Pattern match "website by X 2018" |
| **Phone mismatch** | 20 | Compare Maps data vs website content |

### Proposed Additions (Higher Effort)

| Signal | Weight | Implementation |
|--------|--------|----------------|
| **Google PageSpeed** | variable | API integration (free tier) |
| **Not indexed** | 25 | Google Custom Search API |
| **Competitor gap** | context | Compare to top 3 in same niche/city |

### Signal Stacking Bonuses

Certain combinations indicate stronger leads:

| Combination | Bonus | Rationale |
|-------------|-------|-----------|
| Old copyright + no viewport | +15 | Classic neglected site |
| DIY builder + slow + no HTTPS | +20 | Maxed out free tier |
| Broken images + old copyright | +20 | Owner doesn't check own site |
| Advertising detected + issues | +25 | Wasting ad spend on broken site |

---

## 3. Data Enrichment

### Business Intelligence

| Data Point | Source | Value |
|------------|--------|-------|
| **Review count** | Google Maps (already scraping) | Business size/volume proxy |
| **Review recency** | Google Maps | Active vs dormant indicator |
| **Star rating** | Google Maps | Quality indicator |
| **Years in business** | Google/website "Est. XXXX" | Stability indicator |
| **Domain age** | WHOIS lookup | Established vs new |
| **Advertising spend** | GTM/FB Pixel/gclid detection | Marketing budget indicator |

### Lead Qualification

| Signal | Interpretation |
|--------|----------------|
| 200+ reviews | High volume, likely has budget |
| 10-50 reviews | Established local, good target |
| <10 reviews | Very small or new |
| GTM/FB Pixel detected | Already spending on marketing |
| "Licensed and insured" | Legitimate, professional |
| Multiple locations | Chain/franchise (different pitch) |

### Contact Enrichment

| Data Point | Source | Value |
|------------|--------|-------|
| Owner/decision-maker name | Website about page, LinkedIn | Personalization |
| Email pattern | Hunter.io API | Direct outreach |
| Phone validation | Twilio Lookup | Avoid dead numbers |

---

## 4. Market Intelligence

### Per-City/Niche Reports

Generate aggregate insights for subscribers:

```
Austin, TX - Plumbers (Weekly Summary)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total businesses scraped:     127
With broken/outdated sites:    34 (27%)
Average review score:         4.2★
Common issues:
  - No HTTPS:                  45%
  - Outdated copyright:        38%
  - No mobile viewport:        29%
Top competitors (by reviews):
  1. ABC Plumbing (412 reviews, 4.8★)
  2. XYZ Pipes (298 reviews, 4.6★)
  3. Quick Fix Plumbing (201 reviews, 4.7★)
```

### Competitive Analysis

For each lead, include:
- Top 3 competitors in same niche/city
- Whether competitors have modern sites
- Platform competitors use (WordPress, Squarespace, custom)

### Seasonal Timing

| Niche | Peak Season | Best Outreach Window |
|-------|-------------|---------------------|
| HVAC | Summer/Winter | Spring, Fall |
| Roofers | Spring/Summer | Late Winter |
| Landscaping | Spring-Fall | Late Winter |
| Tax preparers | Jan-Apr | November |
| Pool services | Summer | Early Spring |

Include in output: "Peak season approaching - good time to upgrade"

---

## 5. Product Enhancements

### Subscriber Personalization

Allow subscribers to configure:
- Niche preferences (include/exclude)
- Geographic focus (cities)
- Minimum business size (by review count)
- Exclude chains/franchises
- Lead tier preferences (hot only, all)

### Lead Tiers

| Tier | Score | Label | Recommended Action |
|------|-------|-------|-------------------|
| Hot | 80+ | Site broken/urgent | Call immediately |
| Warm | 60-79 | Site outdated | Email within week |
| Cool | 40-59 | Minor issues | Drip campaign |
| Skip | <40 | Site acceptable | Don't contact |

### Pitch Suggestions

Auto-generate opening line based on detected issues:

| Issues Found | Suggested Pitch |
|--------------|-----------------|
| SSL expiring | "I noticed your security certificate expires in X days..." |
| Broken images | "I was checking out your site and noticed some images aren't loading..." |
| No mobile | "I tried viewing your site on my phone and it was difficult to navigate..." |
| Old copyright | "I see your website still shows 2019 in the footer..." |
| Phone mismatch | "Your Google listing shows a different phone number than your website..." |

### Output Enhancements

Current CSV columns:
```
name, website, score, reasons, address, phone, city, category, place_id
```

Proposed additions:
```
review_count, star_rating, business_age_years, tier, suggested_pitch,
has_advertising, ssl_expires_days, competitor_1, competitor_2, competitor_3
```

### Premium Features (Future)

| Feature | Price Point | Description |
|---------|-------------|-------------|
| Exclusive leads | +$50/mo | Leads not sent to other subscribers for 7 days |
| Real-time alerts | +$25/mo | Email when hot lead discovered (not weekly batch) |
| CRM integration | +$25/mo | Direct push to HubSpot/Pipedrive |
| Custom niches | +$25/mo | Request specific business types |

---

## 6. Implementation Priority

### Phase 1: Quick Wins (Week 1)

Low effort, immediate impact:

- [ ] Score slow response time (already have data)
- [ ] Score redirect chain length
- [ ] Check Last-Modified header
- [ ] Extract review count from Maps
- [ ] Fix CSV injection vulnerability
- [ ] Remove "near me" from queries
- [ ] Move `import time` to module level

#### Phase 1 Detailed Task List (handoff-ready)

**Scoring quick wins**
- [ ] Add `slow_response_ms_threshold` + `slow_response_weight` to `config.py`, wire into `scoring.py` to score when `response_time_ms` exceeds threshold.
- [ ] Track redirect count in `scoring.py` using `len(response.history)` and add configurable weight.
- [ ] Parse `Last-Modified` header; add helper to compute age in years and score if > configured cutoff.
- [ ] Add guards to avoid double-scoring and ensure reasons list includes human-readable text for each new signal.

**Maps data extraction**
- [ ] Extend `maps_scraper.py` result payload to include `review_count` (and validate it is persisted in `db.py`).
- [ ] Update `delivery.py` export columns to include `review_count` and add to CSV output ordering.

**Data safety + cleanup**
- [ ] Sanitize CSV values in `delivery.py` to prevent formula injection (prefix `'` for `=`, `+`, `-`, `@`).
- [ ] Remove "near me" from query composition in `config.py` when city is specified.
- [ ] Move `import time` to module top in `scoring.py` and ensure lints/tests still pass.

**Validation**
- [ ] Add/extend unit tests for new scoring signals.
- [ ] Run `--validate` mode to ensure no runtime regressions.

### Phase 2: Core Improvements (Week 2-3)

Medium effort, high value:

- [ ] SSL certificate expiry check
- [ ] Broken image detection
- [ ] Missing contact info detection
- [ ] SEO basics check (title, meta, h1)
- [ ] Advertising spend detection (GTM/FB Pixel)
- [ ] Expand "under construction" patterns
- [ ] Fix database race condition
- [ ] Fix Playwright resource leak
- [ ] Add unit tests for scoring

#### Phase 2 Detailed Task List (handoff-ready)

**Reliability + correctness**
- [ ] Fix DB race condition by wrapping duplicate check + upsert in a single transaction; add a unique index on `place_id` if missing.
- [ ] Refactor Playwright usage to context manager pattern (`async with` / `with`) to ensure browser and pages close cleanly.

**Site health detection**
- [ ] Implement SSL expiry lookup (socket + cert parsing); add configurable threshold + weight in `config.py`.
- [ ] Implement broken image detection with a capped sample size (e.g., first 10 images), HEAD requests, timeouts, and weight per broken image.
- [ ] Add missing phone/email detection via regex; include separate weights for each missing item.
- [ ] Add SEO checks: empty/generic `<title>`, missing meta description, missing `<h1>`; make weights configurable.
- [ ] Expand "under construction" / parked domain patterns list and add tests for positive matches.

**Advertising detection**
- [ ] Detect GTM/GA/FB Pixel tags in HTML; flag `has_advertising` in scoring and output.

**Testing**
- [ ] Add unit tests for all new signals and thresholds.
- [ ] Add integration smoke test on a known sample HTML fixture set.

### Phase 3: Enrichment (Week 4-5)

Higher effort, differentiation:

- [ ] Decision-maker name extraction
- [ ] Competitor analysis
- [ ] Phone mismatch detection (Maps vs website)
- [ ] Dead social link checking
- [ ] Lead tier classification
- [ ] Pitch suggestion generation
- [ ] Market saturation reports

#### Phase 3 Detailed Task List (handoff-ready)

**Enrichment data sources**
- [ ] Build HTML-based extractor for owner/decision-maker names from "About" and "Team" sections.
- [ ] Implement competitor scraping from Maps: capture top 3 by reviews in same niche/city.
- [ ] Add phone mismatch detection by normalizing and comparing Maps vs website numbers.
- [ ] Add dead social link checks with HEAD/GET validation and timeouts.

**Lead packaging**
- [ ] Add lead tier classification (Hot/Warm/Cool/Skip) based on score ranges.
- [ ] Implement pitch suggestion generator using detected issues; store suggested pitch text.
- [ ] Add market saturation report generator to produce per-city/niche summaries.

**Exports**
- [ ] Extend CSV schema with new enrichment fields and ensure DB schema includes columns.
- [ ] Update email template to highlight tiers and top insights.

### Phase 4: Product Expansion (Week 6+)

New features:

- [ ] Subscriber preferences system
- [ ] Google PageSpeed API integration
- [ ] Yelp cross-reference
- [ ] Week-over-week change detection
- [ ] Enhanced CSV output format
- [ ] Weekly summary email

#### Phase 4 Detailed Task List (handoff-ready)

**Subscriber personalization**
- [ ] Add preference fields (niche include/exclude, cities, min review count) in subscriber model.
- [ ] Filter lead export per subscriber based on preferences and tiers.
- [ ] Add admin tooling to view/edit subscriber preferences.

**External integrations**
- [ ] Integrate Google PageSpeed API with caching and rate limiting.
- [ ] Add Yelp lookup for business verification and enrichment.

**Product analytics**
- [ ] Implement week-over-week change detection for lead status and scoring deltas.
- [ ] Expand CSV output format to include enrichment, tiers, and pitch suggestions.
- [ ] Add weekly summary email with aggregate metrics + highlights.

---

## Metrics to Track

### Lead Quality

- Conversion rate feedback from subscribers (if available)
- Score distribution histogram
- False positive rate (sites that aren't actually broken)

### System Health

- Scrape success rate per city
- Average leads per run
- Delivery success rate
- Run duration trends

### Business Metrics

- Subscriber retention rate
- Lead count per subscriber per week
- Niche/city coverage gaps

---

## Contributing

When implementing improvements:

1. Create feature branch from `main`
2. Update scoring weights in `config.py` (keep configurable)
3. Add detection logic to `scoring.py`
4. Update CSV output in `delivery.py` if adding columns
5. Add tests for new scoring logic
6. Update this roadmap to mark items complete

---

*Last updated: January 2026*
