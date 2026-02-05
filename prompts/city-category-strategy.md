# Strategy Decision: City & Category Selection for Lead Generation

## Context

I run **BrokenSite-Weekly**, an automated lead-generation system for web designers/agencies. It finds local businesses with broken or outdated websites and delivers them as leads to subscribers.

### How it works

1. Scrapes Google Maps for businesses in specific **cities** and **categories**
2. Visits each business's website and scores it for problems (SSL errors, outdated design, parked domains, no mobile support, etc.)
3. Businesses scoring above a threshold become leads
4. Leads are delivered weekly via email to paying subscribers (Gumroad product)

### Current selection (hardcoded, no rationale)

**10 cities:** Austin TX, Denver CO, Phoenix AZ, Nashville TN, Charlotte NC, Portland OR, San Antonio TX, Jacksonville FL, Columbus OH, Indianapolis IN

**10 categories:** plumber, electrician, hvac repair, roofing contractor, landscaping service, auto repair shop, dentist, chiropractor, hair salon, restaurant

### What I know from a recent dry run (Austin + partial Denver, ~580 businesses scraped)

- 18 leads found from ~580 businesses (~3% hit rate)
- Strongest signals: DNS failures (score 95), SSL errors (80), no HTTPS + no viewport + not responsive (65)
- 5 of 18 leads were "social_only" (Facebook/Instagram as their website) - these may or may not be useful to subscribers
- "restaurant" category produced 0 leads from 49 businesses in Austin
- Home services trades (plumber, electrician, HVAC, roofing, landscaping) produced the most leads

### Constraints

- Each city+category query takes ~4 minutes (scraping + scoring), so 100 queries = ~7 hours
- System runs weekly on a VPS via systemd timer
- Subscribers are web designers/agencies who want local business leads they can cold-pitch for website redesigns
- Budget is minimal (VPS cost only, no paid data sources)

## What I need help deciding

### 1. City Selection Strategy

- What criteria should I use to pick cities? (population size, growth rate, competition density, subscriber location, small business density?)
- Should cities rotate weekly or stay fixed?
- How many cities per run is optimal given the ~7 hour runtime constraint?
- Should I let subscribers choose their cities, or curate a list?

### 2. Category Selection Strategy

- Which business categories are most likely to have outdated websites AND be receptive to a cold pitch for a redesign?
- Should I drop categories with low hit rates (like "restaurant")?
- Are there high-value categories I'm missing?
- Should categories be weighted differently (more queries for high-yield categories)?

### 3. Scaling & Rotation

- If I can only run 100 queries per week, what's the best allocation between cities and categories?
- Would 20 cities x 5 categories be better than 10 x 10? Or 5 x 20?
- Should I implement a rotation scheme so I cover more ground over a month?

### 4. Subscriber Value

- What makes a lead valuable to a web designer doing cold outreach?
- Should I focus on depth (more businesses per city) or breadth (more cities)?
- Are "social_only" leads (businesses using Facebook as their website) valuable or noise?

## What good output looks like

I want a concrete recommendation I can implement, not just frameworks. Specifically:

- A ranked list of recommended categories with reasoning
- Criteria for city selection with 10-15 specific city recommendations
- A rotation strategy if applicable
- What to cut and what to add from the current lists
