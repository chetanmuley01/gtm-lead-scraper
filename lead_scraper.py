"""
Lead List Scraper — build a clean, deduplicated company list from live job postings,
with a *verified* domain for each company.

Pointed at companies hiring GTM Engineers, so the output doubles as a real lead list.
Emits CSV that drops straight into email_finder.py.

The interesting problem is not scraping. Job boards give you a company *name* and nothing
else. A name is not addressable - "Acme Technologies, Inc." cannot be enriched, emailed
or matched against a CRM. Turning it into a verified domain is the whole job, and it is
exactly the entity-resolution step that sits under every GTM data pipeline.

Usage:
    python lead_scraper.py --out leads.csv
    python lead_scraper.py --query "revenue operations" --limit 40
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from typing import Dict, Iterable, List, Optional

import dns.resolver

UA = {"User-Agent": "gtm-lab-lead-scraper/1.0 (+https://github.com/chetanmuley01)"}
TIMEOUT = 25

KEYWORD_TERMS = [
    "gtm", "go-to-market", "go to market", "revops", "revenue operations",
    "sales engineer", "growth engineer", "marketing engineer", "sales operations",
]

# Stripped before guessing a domain. Order matters: longest first.
LEGAL_SUFFIXES = [
    "technologies", "technology", "solutions", "software", "labs", "group", "holdings",
    "international", "worldwide", "systems", "digital", "studio", "studios", "agency",
    "limited", "ltd", "llc", "inc", "corp", "corporation", "co", "gmbh", "bv", "ab",
    "oy", "sa", "srl", "pte", "pty", "plc", "ag", "as", "nv",
]

# .ai before .co/.so: modern startups skew .ai, and a .com-first bias produces
# confidently wrong answers (sardine.com is a parked page; the company is sardine.ai).
TLDS = [".com", ".io", ".ai", ".co", ".so", ".dev", ".app", ".net"]


@dataclass
class Lead:
    company: str
    domain: str
    domain_status: str      # verified | unresolved
    role: str
    location: str
    industry: str
    source: str
    posted: str
    url: str
    first: str = ""         # left blank on purpose - email_finder.py fills these
    last: str = ""


def get_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def matches(*fields) -> bool:
    """
    Sources disagree on shape: Remotive returns `tags` as a real list, RemoteOK as a
    string. Coerce everything before joining rather than trusting either.
    """
    parts = []
    for f in fields:
        if not f:
            continue
        parts.append(" ".join(map(str, f)) if isinstance(f, (list, tuple)) else str(f))
    hay = " ".join(parts).lower()
    return any(term in hay for term in KEYWORD_TERMS)


# --- sources -----------------------------------------------------------------

def fetch_remotive(query: str) -> List[Lead]:
    """
    Remotive is kept as a source but expect little from it.

    Measured 2026-09-12: the public API **silently ignores `?search=`**. Five different
    search terms returned the identical 16 job IDs, and none passed KEYWORD_TERMS. It
    serves a generic recent-jobs slice, so the local filter is the only real gate.
    """
    out = []
    try:
        data = get_json("https://remotive.com/api/remote-jobs")
    except Exception as e:
        print(f"  Remotive failed: {e}", file=sys.stderr)
        return out
    for j in data.get("jobs", []):
        if not matches(j.get("title"), j.get("tags"), j.get("category")):
            continue
        out.append(Lead(
            company=(j.get("company_name") or "").strip(),
            domain="", domain_status="",
            role=(j.get("title") or "").strip(),
            location=(j.get("candidate_required_location") or "").strip(),
            industry=(j.get("category") or "").strip(),
            source="Remotive",
            posted=(j.get("publication_date") or "")[:10],
            url=j.get("url") or "",
        ))
    return out


def fetch_remoteok(query: str) -> List[Lead]:
    """RemoteOK has no search parameter - pull the feed and filter everything."""
    out = []
    try:
        data = get_json("https://remoteok.com/api")
    except Exception as e:
        print(f"  RemoteOK failed: {e}", file=sys.stderr)
        return out
    for j in data:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        if not matches(j.get("position"), j.get("tags")):
            continue
        out.append(Lead(
            company=(j.get("company") or "").strip(),
            domain="", domain_status="",
            role=(j.get("position") or "").strip(),
            location=(j.get("location") or "").strip() or "Remote",
            industry="",
            source="RemoteOK",
            posted=(j.get("date") or "")[:10],
            url=j.get("url") or j.get("apply_url") or "",
        ))
    return out


def fetch_jobicy(query: str) -> List[Lead]:
    out = []
    try:
        data = get_json("https://jobicy.com/api/v2/remote-jobs?count=50")
    except Exception as e:
        print(f"  Jobicy failed: {e}", file=sys.stderr)
        return out
    for j in data.get("jobs", []):
        if not matches(j.get("jobTitle"), j.get("jobIndustry"), j.get("jobExcerpt")):
            continue
        out.append(Lead(
            company=(j.get("companyName") or "").strip(),
            domain="", domain_status="",
            role=(j.get("jobTitle") or "").strip(),
            location=(j.get("jobGeo") or "").strip(),
            industry=", ".join(j.get("jobIndustry") or []),
            source="Jobicy",
            posted=(j.get("pubDate") or "")[:10],
            url=j.get("url") or "",
        ))
    return out


def fetch_arbeitnow(query: str) -> List[Lead]:
    """Biggest free pool by an order of magnitude - 250 rows per page."""
    out = []
    try:
        data = get_json("https://www.arbeitnow.com/api/job-board-api")
    except Exception as e:
        print(f"  Arbeitnow failed: {e}", file=sys.stderr)
        return out
    for j in data.get("data", []):
        if not matches(j.get("title"), j.get("tags"), j.get("description")):
            continue
        ts = j.get("created_at")
        posted = ""
        if isinstance(ts, (int, float)):
            from datetime import datetime, timezone
            posted = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        out.append(Lead(
            company=(j.get("company_name") or "").strip(),
            domain="", domain_status="",
            role=(j.get("title") or "").strip(),
            location=(j.get("location") or "").strip() or "Remote",
            industry=", ".join(j.get("tags") or []),
            source="Arbeitnow",
            posted=posted,
            url=j.get("url") or "",
        ))
    return out


def fetch_himalayas(query: str) -> List[Lead]:
    out = []
    try:
        data = get_json("https://himalayas.app/jobs/api?limit=50")
    except Exception as e:
        print(f"  Himalayas failed: {e}", file=sys.stderr)
        return out
    for j in data.get("jobs", []):
        if not matches(j.get("title"), j.get("excerpt"), j.get("seniority")):
            continue
        out.append(Lead(
            company=(j.get("companyName") or "").strip(),
            domain="", domain_status="",
            role=(j.get("title") or "").strip(),
            location=", ".join(j.get("locationRestrictions") or []) or "Remote",
            industry=(j.get("employmentType") or "").strip(),
            source="Himalayas",
            # Field name varies across their responses - try both, accept neither.
            posted=(j.get("pubDate") or j.get("datePosted") or "")[:10],
            url=j.get("applicationLink") or j.get("url") or "",
        ))
    return out


# --- the actual problem: name -> verified domain ------------------------------

_dns_cache: Dict[str, bool] = {}


def resolves(domain: str) -> bool:
    """A domain is real if it has an MX or an A record. Cached - names repeat."""
    if domain in _dns_cache:
        return _dns_cache[domain]
    ok = False
    for rtype in ("MX", "A"):
        try:
            dns.resolver.resolve(domain, rtype)
            ok = True
            break
        except Exception:
            continue
    _dns_cache[domain] = ok
    return ok


def domain_candidates(company: str) -> List[str]:
    """
    "Acme Technologies, Inc." -> acme.com, acme.io, acmetechnologies.com, ...

    Two shapes are tried: the name with legal/industry suffixes stripped, and the full
    name. Stripping usually wins ("Copenhagen Optimization" -> copenhagenoptimization.com
    is right, but "Stripe Inc" -> stripe.com needs the strip), so both are generated and
    DNS decides.
    """
    base = re.sub(r"[^\w\s-]", " ", (company or "").lower())
    words = [w for w in base.split() if w]
    if not words:
        return []

    stripped = [w for w in words if w not in LEGAL_SUFFIXES] or words
    forms = []
    for parts in (stripped, words):
        joined = "".join(parts)
        if joined and joined not in forms:
            forms.append(joined)
        hyphen = "-".join(parts)
        if len(parts) > 1 and hyphen not in forms:
            forms.append(hyphen)

    return [f"{form}{tld}" for form in forms for tld in TLDS]


HTTP_TIMEOUT = 5

# A coined name ("databricks") that exactly matches a domain is near-proof of ownership.
# A dictionary word ("relay", "sardine") is not: common words were bought decades ago by
# whoever got there first. relay.com is a French retailer; sardine.com is parked while the
# real company is sardine.ai. This list is the gate between those two cases.
_DICT_PATHS = ["/usr/share/dict/words", "/usr/dict/words"]
# Fallback when no system word list exists. Deliberately short: without a real list the
# safe behaviour is to trust exact matches less, not more, so unknown words fall through
# to a content check rather than being waved past.
_FALLBACK_WORDS = {
    "relay", "sardine", "wordsmith", "apple", "orange", "square", "stripe", "slack",
    "notion", "figma", "amber", "canvas", "beacon", "anchor", "harvest", "ramp", "lever",
    "front", "loom", "miro", "arc", "atlas", "compass", "pilot", "signal", "spark",
    "flow", "pulse", "vault", "forge", "bolt", "drift", "mercury", "monarch", "otter",
}


def _load_words() -> set:
    for path in _DICT_PATHS:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                return {w.strip().lower() for w in fh if len(w.strip()) >= 3}
        except OSError:
            continue
    return _FALLBACK_WORDS


COMMON_WORDS = _load_words()
HAVE_REAL_DICT = len(COMMON_WORDS) > 1000
MAX_CONTENT_CHECKS = 2   # hard cap per company - see resolve_domain()


def page_title(domain: str) -> Optional[str]:
    """
    Fetch the homepage and return its <title>. None if unreachable.

    HTTPS only. An http:// fallback doubles the worst case for almost no yield - a
    company whose site refuses HTTPS in 2026 is not one this pipeline needs to confirm.
    """
    try:
        req = urllib.request.Request("https://" + domain, headers={
            "User-Agent": "Mozilla/5.0 (compatible; gtm-lab/1.0)"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            html = r.read(60000).decode("utf-8", errors="replace")
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    except Exception:
        return None


def name_matches_page(company: str, title: Optional[str], domain: str) -> Optional[bool]:
    """
    True  - the page looks like this company.
    False - the page clearly belongs to someone else.
    None  - cannot tell (unreachable, bot challenge, empty title).
    """
    if title is None:
        return None
    low = title.lower()
    if not low or "just a moment" in low or low == domain.lower():
        return None            # Cloudflare challenge, or a parked placeholder
    tokens = [w for w in re.sub(r"[^a-z0-9 ]", " ", company.lower()).split()
              if w not in LEGAL_SUFFIXES and len(w) > 2]
    if not tokens:
        return None
    return any(tok in low for tok in tokens)


def slug_name(company: str) -> str:
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", (company or "").lower()).split()
             if w not in LEGAL_SUFFIXES]
    return "".join(words)


def is_coined(slug: str) -> bool:
    """
    True when the name is invented rather than borrowed from English.

    "databricks", "zapier", "chainguard" - coined, so an exact domain match is near-proof.
    "relay", "sardine", "wordsmith" - real words, so an exact match proves nothing about
    ownership. Without a real system word list, nothing is treated as coined: the safe
    failure is to under-confirm.
    """
    return HAVE_REAL_DICT and bool(slug) and slug not in COMMON_WORDS


def has_mx(domain: str) -> bool:
    """Operating companies receive mail. Parked domains usually do not."""
    try:
        dns.resolver.resolve(domain, "MX")
        return True
    except Exception:
        return False


def resolve_domain(company: str, *, check_content: bool = True) -> tuple[str, str]:
    """
    DNS proves a domain exists. It does not prove this company owns it.

    Three signals, combined rather than short-circuited - the earlier version promoted
    exact-match to proof because it made the number go up, and silently re-broke every
    company whose name is an ordinary English word.

      confirmed   - coined name matching its domain, or page content naming the company
      likely      - exact match on a dictionary word, domain has MX, content unconfirmed
      unconfirmed - resolves, nothing corroborates ownership. Do not mail these.
      unresolved  - no candidate resolved
    """
    live = [c for c in domain_candidates(company) if resolves(c)]
    if not live:
        return "", "unresolved"

    slug = slug_name(company)
    exact = next((c for c in live if c.rsplit(".", 1)[0] == slug), "") if slug else ""

    # Signal 1: coined name + exact match. No network call needed.
    if exact and is_coined(slug):
        return exact, "confirmed"

    # Signal 2: page content actually names the company.
    #
    # Deliberately NOT applied to dictionary-word names. relay.com belongs to a French
    # retailer that is also called Relay, so its homepage genuinely contains "relay" and
    # the check confirms the wrong company with perfect internal logic. When the name is
    # an ordinary word, finding that word on the page is circular evidence. Two real
    # companies can share a real word and no amount of scraping separates them, so the
    # ceiling for these is "likely" and a human decides.
    if check_content and is_coined(slug):
        for cand in ([exact] if exact else [])[:1] + live[:MAX_CONTENT_CHECKS]:
            if name_matches_page(company, page_title(cand), cand) is True:
                return cand, "confirmed"

    # Signal 3: a dictionary-word name that at least runs mail. Evidence, not proof.
    if exact and has_mx(exact):
        return exact, "likely"

    return live[0], "unconfirmed"


# --- pipeline -----------------------------------------------------------------

def dedupe(leads: Iterable[Lead]) -> List[Lead]:
    """One row per company. Keep the first posting seen; job boards mirror each other."""
    seen: Dict[str, Lead] = {}
    for l in leads:
        key = re.sub(r"[^a-z0-9]", "", l.company.lower())
        if not key or key in seen:
            continue
        seen[key] = l
    return list(seen.values())


def run(query: str, limit: int, out_path: str, no_check: bool = False) -> None:
    print(f"Searching for: {query!r}")
    sources = [fetch_remotive, fetch_remoteok, fetch_jobicy, fetch_arbeitnow, fetch_himalayas]
    raw = []
    for fn in sources:
        got = fn(query)
        print(f"  {fn.__name__.replace('fetch_', ''):<12} {len(got):>3} matching")
        raw.extend(got)
    print(f"  {len(raw)} matching postings across {len(sources)} sources")

    leads = dedupe(raw)[:limit]
    print(f"  {len(leads)} unique companies after dedup\n")

    print("Resolving domains...")
    for l in leads:
        l.domain, l.domain_status = resolve_domain(l.company, check_content=not no_check)
        mark = {"confirmed": "OK  ", "likely": "~   ", "unconfirmed": "?   ",
                "unresolved": "--  "}.get(l.domain_status, "    ")
        print(f"  {mark}{l.company[:32]:<32} {l.domain or '(unresolved)':<28} {l.domain_status}", flush=True)

    if not leads:
        print("\nNo companies found. Try a broader --query.")
        return

    fields = list(asdict(leads[0]).keys())
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for l in leads:
            w.writerow(asdict(l))

    from collections import Counter
    counts = Counter(l.domain_status for l in leads)
    n = len(leads)
    print(f"\nWrote {out_path}\n")
    print(f"  confirmed    {counts['confirmed']:>3}/{n}  coined name matches domain, or page names the company")
    print(f"  likely       {counts['likely']:>3}/{n}  dictionary-word name, exact match, has MX - evidence only")
    print(f"  unconfirmed  {counts['unconfirmed']:>3}/{n}  resolves, nothing corroborates ownership")
    print(f"  unresolved   {counts['unresolved']:>3}/{n}  no candidate resolved")
    print("\nOnly 'confirmed' is safe to mail. 'likely' needs a human glance: a common-word")
    print("name matching a .com is exactly how you end up emailing a French retailer.")
    if not HAVE_REAL_DICT:
        print("WARNING: no system word list found - nothing treated as coined, so this run")
        print("under-confirms by design.")


def main() -> None:
    p = argparse.ArgumentParser(description="Scrape a company lead list from job postings.")
    p.add_argument("--query", default="GTM Engineer")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--out", default="leads.csv")
    p.add_argument("--no-check", action="store_true",
                   help="Skip the HTTP content check (faster, but domains stay unconfirmed)")
    a = p.parse_args()
    run(a.query, a.limit, a.out, a.no_check)


if __name__ == "__main__":
    main()
