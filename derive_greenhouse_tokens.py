"""
Derive-and-verify Greenhouse board tokens from a TheirStack Excel export.

For each company:
  1. Generate ordered candidate slugs (domain-based, name-based, suffix-stripped, parent-extracted).
  2. Hit https://boards-api.greenhouse.io/v1/boards/<slug>/jobs.
  3. First 200 OK wins. Compare returned job count against TheirStack's
     greenhouse_n_jobs as a confidence signal.

No credits spent — Greenhouse Board API is public + free.
"""

import csv
import json
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import openpyxl

INPUT_PATH = "/Users/glennfiocca/Desktop/Greenhouse Export List Test.xlsx"
HITS_CSV = "/Users/glennfiocca/Desktop/greenhouse_hits.csv"
MISSES_CSV = "/Users/glennfiocca/Desktop/greenhouse_misses.csv"
# content=false drops the heavy job-description payload — much faster for big boards
GH_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
REQUEST_TIMEOUT = 30
REQUEST_DELAY_SEC = 0.25  # be polite to greenhouse
MAX_RETRIES = 2

COMMON_SUFFIXES = [
    "inc", "incorporated", "corp", "corporation", "llc", "ltd", "limited",
    "company", "co", "group", "international", "industries", "holdings",
    "global", "worldwide", "the",
]


@dataclass
class Row:
    name: str
    url: Optional[str]
    gh_confidence: Optional[str]
    gh_n_jobs: Optional[int]
    candidates: list[str] = field(default_factory=list)
    resolved_slug: Optional[str] = None
    resolved_job_count: Optional[int] = None
    tried: list[tuple[str, int]] = field(default_factory=list)  # (slug, http_status)


def normalize(s: str) -> str:
    """Lowercase, strip diacritics, keep only [a-z0-9]."""
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    return re.sub(r"[^a-z0-9]", "", s)


def domain_slug(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    if not m:
        return None
    host = m.group(1)
    # take SLD (second-level domain) — handles "varsitytutors.com" → "varsitytutors"
    parts = host.split(".")
    if len(parts) < 2:
        return normalize(host)
    return normalize(parts[-2])


def parent_company_slug(name: str) -> Optional[str]:
    """Catch patterns like 'Varsity Tutors, a Nerdy Company' → 'nerdy'."""
    m = re.search(r",\s*an?\s+(.+?)\s+(?:company|corp(?:oration)?|inc|llc)\b",
                  name, re.IGNORECASE)
    if m:
        return normalize(m.group(1))
    return None


def strip_suffixes(name: str) -> str:
    tokens = re.split(r"\s+", name.strip())
    while tokens and tokens[-1].lower().strip(",.") in COMMON_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def first_word_slug(name: str) -> Optional[str]:
    tokens = [t for t in re.split(r"\s+", name.strip()) if t]
    if not tokens:
        return None
    return normalize(tokens[0])


SUFFIX_VARIANTS = ["usa", "us", "inc", "llc", "careers", "jobs", "corp"]


def generate_candidates(name: str, url: Optional[str]) -> list[str]:
    cands: list[str] = []

    def add(s: Optional[str]) -> None:
        if s and s not in cands and len(s) >= 2:
            cands.append(s)

    # 1) domain-based
    add(domain_slug(url))

    # 2) full name normalized
    add(normalize(name))

    # 3) suffix-stripped (e.g., "BAYADA Home Health Care" -> stays the same since no common suffix)
    add(normalize(strip_suffixes(name)))

    # 4) parent company extracted ("..., a Nerdy Company" -> "nerdy")
    add(parent_company_slug(name))

    # 5) first word
    add(first_word_slug(name))

    # 6) name minus everything after the first comma
    pre_comma = name.split(",")[0]
    add(normalize(pre_comma))
    add(normalize(strip_suffixes(pre_comma)))

    # 7) appended suffix variants (DoorDash -> doordashusa, etc.)
    base_for_suffix = [
        normalize(strip_suffixes(pre_comma)),
        normalize(pre_comma),
        first_word_slug(name),
        domain_slug(url),
    ]
    seen_bases: set[str] = set()
    for base in base_for_suffix:
        if not base or base in seen_bases:
            continue
        seen_bases.add(base)
        for suffix in SUFFIX_VARIANTS:
            add(base + suffix)

    return cands


def probe_greenhouse(slug: str) -> tuple[int, Optional[int]]:
    """Return (http_status, jobs_count_or_None). 0 = network failure after retries."""
    req = urllib.request.Request(
        GH_API.format(slug=slug),
        headers={"User-Agent": "launchpad-jobs-token-verifier/1.0"},
    )
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.load(resp)
                jobs = data.get("jobs") or []
                return resp.status, len(jobs)
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                json.JSONDecodeError, ConnectionError):
            if attempt < MAX_RETRIES:
                time.sleep(1.0 * (attempt + 1))
                continue
            return 0, None
    return 0, None


def load_rows(path: str) -> list[Row]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    headers = next(rows_iter)
    idx = {h: i for i, h in enumerate(headers)}
    out: list[Row] = []
    for r in rows_iter:
        if r is None or all(v is None for v in r):
            continue
        out.append(Row(
            name=str(r[idx["company_name"]] or "").strip(),
            url=(r[idx["company_url"]] or None),
            gh_confidence=r[idx.get("greenhouse_confidence", -1)] if "greenhouse_confidence" in idx else None,
            gh_n_jobs=r[idx["greenhouse_n_jobs"]] if "greenhouse_n_jobs" in idx else None,
        ))
    return out


def resolve(row: Row) -> Row:
    row.candidates = generate_candidates(row.name, row.url)
    for slug in row.candidates:
        status, jobs = probe_greenhouse(slug)
        row.tried.append((slug, status))
        if status == 200:
            row.resolved_slug = slug
            row.resolved_job_count = jobs
            break
        time.sleep(REQUEST_DELAY_SEC)
    return row


def classify_confidence(row: Row) -> str:
    if not row.resolved_slug:
        return "miss"
    if row.gh_n_jobs is None or row.resolved_job_count is None:
        return "hit (unverified count)"
    if row.resolved_job_count == 0 and row.gh_n_jobs > 0:
        return "hit (zero jobs returned — suspect)"
    if row.gh_n_jobs == 0:
        return "hit (no theirstack count to compare)"
    ratio = row.resolved_job_count / row.gh_n_jobs
    if 0.3 <= ratio <= 3.0:
        return "hit (counts close)"
    return f"hit (count mismatch: gh={row.resolved_job_count}, ts={row.gh_n_jobs})"


def main() -> int:
    rows = load_rows(INPUT_PATH)
    print(f"Loaded {len(rows)} companies from {INPUT_PATH}\n")

    hit_count = 0
    for i, row in enumerate(rows, 1):
        resolve(row)
        verdict = classify_confidence(row)
        if row.resolved_slug:
            hit_count += 1
        print(f"[{i:>2}] {row.name[:42]:<42} "
              f"slug={row.resolved_slug or '—':<22} "
              f"gh_jobs={row.resolved_job_count!s:<5} "
              f"ts_jobs={row.gh_n_jobs!s:<6} {verdict}")
        if not row.resolved_slug:
            tried_str = ", ".join(f"{s}({st})" for s, st in row.tried)
            print(f"      tried: {tried_str}")

    print()
    print(f"Hits: {hit_count}/{len(rows)} ({100*hit_count/len(rows):.0f}%)")

    # write hits (seedable rows) and misses (manual review queue) to CSV
    with Path(HITS_CSV).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "board_token", "company_url", "gh_active_jobs", "ts_historical_jobs"])
        for r in rows:
            if r.resolved_slug:
                w.writerow([r.name, r.resolved_slug, r.url or "", r.resolved_job_count, r.gh_n_jobs])
    with Path(MISSES_CSV).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "company_url", "ts_historical_jobs", "candidates_tried"])
        for r in rows:
            if not r.resolved_slug:
                w.writerow([r.name, r.url or "", r.gh_n_jobs,
                            "; ".join(f"{s}({st})" for s, st in r.tried)])
    print(f"Hits  → {HITS_CSV}")
    print(f"Misses → {MISSES_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
