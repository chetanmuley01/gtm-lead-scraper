# GTM Lead Scraper

Turns live job postings into a clean company list with a **verified domain** for each one.
Pointed at companies hiring GTM Engineers, so the output doubles as a real lead list.

![Pipeline](pipeline.gif)

Emits CSV that feeds straight into a companion
[email finder](#chaining-into-the-email-finder) with no glue code.

## The actual problem

Scraping is the easy part. Job boards hand you a company *name* and nothing else, and a
name is not addressable — you cannot enrich it, mail it, or match it to a CRM record.
`"Acme Technologies, Inc."` has to become `acme.com`, and that step is entity resolution,
which is where every GTM data pipeline actually lives or dies.

## DNS proves existence, not ownership

The naive version guesses candidate domains, checks DNS, and reports a match rate. First
run scored 92%. Then I opened a few:

| Guess | Reality |
|---|---|
| `relay.com` | A French retail chain celebrating 25 years. Not the company in the posting. |
| `sardine.com` | A parked page. The real company is `sardine.ai`. |
| `aweber.com` | Correct. |

So 92% measured whether *a* domain exists, not whether *this company* owns it. Two
different questions, and only one of them matters. **A confidently wrong domain is worse
than a blank: a blank costs you a lead, a wrong one sends your outbound to a stranger.**

## How it decides

Three signals, combined rather than short-circuited.

**1 · Coined name matching its domain.** `databricks` → `databricks.com` is near-proof —
nobody else owns an invented word. Checked against the system word list, because the same
rule applied to `relay` confirms a French retailer. A coined name is evidence; a
dictionary word is a coincidence waiting to happen.

**2 · Page content naming the company.** Fetches the homepage and looks for the company
name in the title, `og:site_name` and meta description. Titles alone are marketing copy —
Databricks' homepage does not contain the word "Databricks".

Deliberately **not** applied to dictionary-word names. The French Relay's homepage
genuinely says "Relay", so the check confirms the wrong company with perfect internal
logic. When the name is an ordinary word, finding that word on the page is circular
evidence.

**3 · MX records.** Operating companies receive mail; parked domains usually do not.
Evidence, never proof.

### Verdicts

| Verdict | Meaning | Safe to mail? |
|---|---|---|
| `confirmed` | Coined name matches domain, or page names the company | Yes |
| `likely` | Dictionary-word name, exact match, has MX | Human glance first |
| `unconfirmed` | Resolves, nothing corroborates ownership | No |
| `unresolved` | No candidate resolved | No |

Measured on a live run of 20 companies: **15 confirmed, 2 likely, 1 unconfirmed,
2 unresolved.** No single headline percentage is quoted, because flattening those four
into one number is exactly the mistake this tool exists to avoid.

A 7-case ground-truth set guards the regression: Databricks, Zapier, Chainguard and
AWeber must confirm; Relay, Sardine and Wordsmith must not.

## Sources, and one worth knowing about

Five free job boards, no API keys. Yield is a **dated snapshot** — feeds change daily (Arbeitnow alone swung from 32 matches to 17 between 12 and 15 September 2026). Measured on 12 September 2026:

| Source | Matches | Note |
|---|---|---|
| Arbeitnow | 32 | 250 rows per page — the only one that really carries |
| RemoteOK | 6 | No search parameter; full feed, filtered locally |
| Jobicy | 2 | |
| Remotive | 0 | **Its `?search=` is silently ignored** |
| Himalayas | 0 | |

**Remotive's public API ignores the search parameter.** Five different query strings
returned the identical 16 job IDs, none matching GTM terms. Anything relying on Remotive
search is filtering client-side whether it knows it or not.

## Usage

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python lead_scraper.py --limit 25 --out leads.csv
./.venv/bin/python lead_scraper.py --query "revenue operations" --no-check   # skip HTTP
```

## Chaining into the email finder

`leads.csv` includes `gtm_postings` — how many distinct matching roles each company posted, the strongest hiring-intensity signal in the data (Datadog: 4). It also ships `first` and `last` columns deliberately blank, in the exact shape a
companion email finder consumes. Fill in contact names and pipe it straight through —
scraper out, verifier in, no transformation step.

## What I'd build next

- **Cross-check against an enrichment API** on `likely` and `unconfirmed` rows only, so
  credits are spent where inference is weakest instead of uniformly.
- **Favicon and logo hashing** to break dictionary-word collisions that content matching
  cannot.
- **Per-source yield telemetry**, so a board that quietly stops returning results gets
  noticed rather than silently contributing zero.
- **Concurrency on DNS and HTTP** — currently sequential, and it shows on large runs.

---

Built by [Chetan Muley](https://github.com/chetanmuley01).
