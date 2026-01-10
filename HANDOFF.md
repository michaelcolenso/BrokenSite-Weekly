# Mission Brief: BrokenSite-Weekly

> You're not just fixing code. You're building a money machine.

---

## The Opportunity

This is a **fully automated lead-generation system** that finds local businesses with broken, outdated, or neglected websites—and delivers them weekly to web developers who pay $50/month for these leads.

**Current state:** Working MVP generating ~$1,000/month (20 subscribers).

**Your mission:** Turn this into a $10,000/month operation.

The bones are solid. The architecture is production-hardened. What it needs now is someone with **vision** and **hunger** to take it from "works" to "dominates."

---

## What You're Working With

```
/home/user/BrokenSite-Weekly/
├── src/
│   ├── maps_scraper.py    # Playwright scrapes Google Maps
│   ├── scoring.py         # Evaluates websites for "broken" signals
│   ├── db.py              # SQLite persistence
│   ├── delivery.py        # SMTP email with CSV attachments
│   ├── gumroad.py         # Subscriber management
│   └── run_weekly.py      # Orchestrates everything
├── ROADMAP.md             # 42 improvements waiting to be built
└── Claude.md              # Architecture deep-dive
```

**How it works:**
1. Every Sunday at 3am, scrapes Google Maps for local businesses (plumbers, dentists, etc.)
2. Visits each website and scores it for "broken" signals (SSL errors, outdated copyright, no mobile, parked domains)
3. Stores leads in SQLite, dedupes against 90-day window
4. Emails CSV of high-scoring leads to paying Gumroad subscribers

---

## What's Already Been Done

A comprehensive code review identified:
- **14 bugs/code quality issues** (race conditions, resource leaks, security fixes)
- **17 new scoring signals** to detect broken sites better
- **12 data enrichment opportunities** (review counts, advertising detection, competitor analysis)
- **8 product expansion ideas** (lead tiers, pitch suggestions, subscriber preferences)

All documented in `ROADMAP.md` with implementation priority.

---

## Your Mandate

### Think Like a Founder

This isn't a coding exercise. This is a business. Every line of code should answer: **"Does this help subscribers close more deals?"**

The subscribers are web developers and agencies. They're paying for leads. What makes a lead valuable?
- **Actionable:** Clear problem they can pitch ("Your SSL expires in 12 days")
- **Qualified:** Business can afford to pay ($$$, established, advertising already)
- **Timely:** Right moment to reach out (new ownership, peak season coming)
- **Exclusive:** Not the same list everyone else has

### Think Creatively

The current system finds "broken" sites. That's table stakes. What else indicates a business needs web help?

Ideas no one's explored yet:
- **Businesses running Google Ads with broken sites** — literally burning money
- **Competitors with better sites stealing their customers** — fear sells
- **Reviews mentioning website problems** — "couldn't find hours online"
- **Seasonal timing** — roofers need sites updated before spring rush
- **New ownership signals** — "under new management" = rebrand opportunity
- **Domain expiring soon** — WHOIS lookup, ultimate urgency

What else? That's your job to figure out.

### Think at Scale

20 subscribers × $50 = $1,000/month.

What gets us to 200 subscribers?
- **Better leads** (higher quality = word of mouth)
- **More niches** (lawyers? real estate agents? what verticals are underserved?)
- **More cities** (currently 10 cities, there are 19,000+ cities in the US)
- **Premium tiers** (exclusive leads? real-time alerts? CRM integration?)
- **Agency partnerships** (white-label for marketing agencies?)

The marginal cost of adding subscribers is nearly zero. Same scrape, same email, infinite leverage.

---

## Where to Start

### Quick Wins (Do These First)

These are already in the codebase, just need to be scored:

1. **`response_time_ms`** — Already captured but never scored. Slow sites = neglected hosting. Add 20 points for >5 seconds.

2. **`len(response.history)`** — Redirect chain length. 3+ redirects = messy DNS/migration. Add 15 points.

3. **Review count extraction** — Already scraping Maps, just need to grab the "(127 reviews)" text. This is your #1 lead qualification signal.

### High-Impact Features

4. **Broken image detection** — Sample 5 images, HEAD request each. Broken images are *visible* problems = easy pitch for subscribers.

5. **Advertising detection** — Check for Google Tag Manager, Facebook Pixel, `gclid` in URLs. If they're paying for ads but have a broken site, they're bleeding money. Hottest leads possible.

6. **Pitch suggestion column** — Based on issues found, auto-generate an opening line: "I noticed your site's security certificate expires next week..." Subscribers will love this.

---

## The Competitive Moat

Right now, anyone could build this. Here's how to make it defensible:

1. **Data compounding** — Track sites over time. "This site hasn't changed in 18 months" is more valuable than point-in-time scoring.

2. **Feedback loops** — If subscribers could mark leads as "converted" or "junk", you'd build a training set to improve scoring.

3. **Niche expertise** — Deep knowledge of what matters per vertical. A dentist's site needs different things than a plumber's.

4. **Relationship data** — Track which leads each subscriber contacted. Never send the same lead twice. Manage territory.

5. **Speed** — First to contact wins. Real-time alerts when a hot lead appears, not weekly batches.

---

## Code Philosophy

- **Don't over-engineer.** This runs once a week. It doesn't need microservices.
- **Fail gracefully.** One bad business shouldn't crash the whole run.
- **Log everything.** When it runs unattended on a VPS, logs are your eyes.
- **Make it configurable.** Scoring weights in config, not hardcoded.
- **Test the money path.** Scoring logic and delivery are critical. Everything else is nice-to-have.

---

## Resources

| File | Purpose |
|------|---------|
| `ROADMAP.md` | 42 prioritized improvements with implementation notes |
| `Claude.md` | Architecture documentation |
| `RUNBOOK.md` | Operations and troubleshooting |
| `SETUP.md` | VPS deployment guide |

---

## The Ask

Be ambitious. Be creative. Be relentless.

Don't just check boxes on the roadmap. Ask "what would make this 10x more valuable?" and build that.

The best feature might not be on any list yet. It might be something you discover while digging through the code. Follow your curiosity. If you see an opportunity, chase it.

Ship fast. Learn faster. Every improvement compounds.

**Now go build something that prints money.**

---

*Previous session: Completed code review, created ROADMAP.md with 42 improvements, committed to `claude/code-review-improvements-JikRF` branch.*
