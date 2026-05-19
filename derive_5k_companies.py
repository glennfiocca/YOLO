"""
Batch derive-and-verify Greenhouse/Ashby board slugs for the TheirStack
companies CSV. Resumable, append-only output, progress logged.

For each company row:
  1. Generate a small ordered list of candidate slugs (domain, lowercased name,
     suffix-stripped, parent-company-extracted, LinkedIn handle, suffix variants).
  2. Probe each candidate against Greenhouse Board API first, then Ashby
     posting-api. First 200 (with > 0 jobs) wins.
  3. Append to hits.csv (one row per resolved company) OR misses.csv (one row
     per unresolved company, with all candidates tried).
  4. Flush after every company so a crash loses at most one row.

Usage:
    python3 derive_5k_companies.py <input.csv> [--limit N] [--resume]

Output (in ./derive_output/):
    hits.csv     - name, ats, slug, api_jobs, total_jobs_ts, sample_url, ...
    misses.csv   - name, company_url, linkedin_url, candidates_tried
    progress.log - one line per company, for tailing
"""

from __future__ import annotations

import csv
import json
import os
import re
import socket
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlparse


GH_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
REQUEST_TIMEOUT = 15
REQUEST_DELAY_SEC = 0.02   # Greenhouse + Ashby are public endpoints; keep light
MAX_RETRIES = 2
PROBE_BACKOFF_SEC = 1.0
NUM_WORKERS = 8            # Concurrent companies in flight

OUT_DIR = Path("/Users/glennfiocca/YOLO/derive_output")
HITS_PATH = OUT_DIR / "hits.csv"
MISSES_PATH = OUT_DIR / "misses.csv"
PROGRESS_PATH = OUT_DIR / "progress.log"
DONE_PATH = OUT_DIR / "done_companies.txt"

EXISTING_DB_PATH = Path("/Users/glennfiocca/Desktop/greenhouse_boards.xlsx")

COMMON_SUFFIXES = [
    "inc", "incorporated", "corp", "corporation", "llc", "ltd", "limited",
    "company", "co", "group", "international", "industries", "holdings",
    "global", "worldwide", "the", "plc", "ag", "gmbh", "sa",
]
SUFFIX_VARIANTS = ["usa", "us", "inc", "llc", "careers", "jobs", "corp",
                   "global", "hq", "ag", "io"]


def normalize_alnum(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]", "", s)


def domain_slug(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        host = urlparse(url).hostname
    except Exception:
        return None
    if not host:
        return None
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) < 2:
        return normalize_alnum(host)
    return normalize_alnum(parts[-2])


def linkedin_slug(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"linkedin\.com/(?:company|school)/([^/?#]+)", url, re.I)
    if not m:
        return None
    handle = m.group(1).strip("-_")
    return normalize_alnum(handle)


def parent_company_slug(name: str) -> Optional[str]:
    m = re.search(
        r",\s*an?\s+(.+?)\s+(?:company|corp(?:oration)?|inc|llc|holdings?|brand)\b",
        name or "", re.IGNORECASE,
    )
    if m:
        return normalize_alnum(m.group(1))
    return None


def strip_suffixes(name: str) -> str:
    tokens = re.split(r"\s+", (name or "").strip())
    while tokens and tokens[-1].lower().strip(",.&") in COMMON_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def first_word_slug(name: str) -> Optional[str]:
    tokens = [t for t in re.split(r"\s+", (name or "").strip()) if t]
    if not tokens:
        return None
    return normalize_alnum(tokens[0])


def generate_candidates(name: str, url: Optional[str], linkedin_url: Optional[str]) -> list[str]:
    cands: list[str] = []
    seen: set[str] = set()

    def add(s: Optional[str]) -> None:
        if s and len(s) >= 2 and s not in seen:
            seen.add(s)
            cands.append(s)

    add(domain_slug(url))
    add(linkedin_slug(linkedin_url))

    add(normalize_alnum(name))
    add(normalize_alnum(strip_suffixes(name)))
    add(parent_company_slug(name))

    pre_comma = (name or "").split(",")[0]
    add(normalize_alnum(pre_comma))
    add(normalize_alnum(strip_suffixes(pre_comma)))

    add(first_word_slug(name))

    base_for_suffix = [
        normalize_alnum(strip_suffixes(pre_comma)),
        normalize_alnum(pre_comma),
        first_word_slug(name),
        domain_slug(url),
    ]
    suffix_seen: set[str] = set()
    for base in base_for_suffix:
        if not base or base in suffix_seen:
            continue
        suffix_seen.add(base)
        for suf in SUFFIX_VARIANTS:
            add(base + suf)

    return cands


def _probe(url: str, jobs_key: str) -> tuple[int, Optional[int]]:
    req = urllib.request.Request(
        url, headers={"User-Agent": "launchpad-jobs-token-verifier/1.0"},
    )
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.load(resp)
                return resp.status, len(data.get(jobs_key) or [])
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                json.JSONDecodeError, ConnectionError, OSError):
            if attempt < MAX_RETRIES:
                time.sleep(PROBE_BACKOFF_SEC * (attempt + 1))
                continue
            return 0, None
    return 0, None


def probe_greenhouse(slug: str) -> tuple[int, Optional[int]]:
    return _probe(GH_API.format(slug=slug), "jobs")


def probe_ashby(slug: str) -> tuple[int, Optional[int]]:
    return _probe(ASHBY_API.format(slug=slug), "jobs")


def load_existing_slugs() -> set[str]:
    """Load slugs already in launchpad's CompanyBoard table (or its xlsx dump)."""
    if not EXISTING_DB_PATH.exists():
        return set()
    try:
        import openpyxl
        wb = openpyxl.load_workbook(EXISTING_DB_PATH, read_only=True, data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        headers = next(rows)
        idx = {h: i for i, h in enumerate(headers) if h is not None}
        key = "board_token" if "board_token" in idx else "boardToken"
        out: set[str] = set()
        for r in rows:
            if not r:
                continue
            slug = r[idx[key]] if key in idx else None
            if slug:
                out.add(str(slug).strip().lower())
        wb.close()
        return out
    except Exception as e:
        print(f"  warning: could not load existing slug DB: {e}", file=sys.stderr)
        return set()


def load_done_companies() -> set[str]:
    if not DONE_PATH.exists():
        return set()
    return set(line.strip() for line in DONE_PATH.read_text().splitlines() if line.strip())


HITS_COLUMNS = [
    "company_name", "ats", "slug", "api_jobs", "company_url", "linkedin_url",
    "country_code", "total_jobs_ts", "industry", "sample_api_url",
]
MISSES_COLUMNS = [
    "company_name", "company_url", "linkedin_url", "country_code",
    "total_jobs_ts", "industry", "candidates_tried",
]


def ensure_csv(path: Path, columns: list[str]) -> None:
    if not path.exists():
        with path.open("w", newline="") as f:
            csv.writer(f).writerow(columns)


_write_lock = threading.Lock()


def append_hit(name: str, ats: str, slug: str, jobs: int, row: dict) -> None:
    sample = (GH_API if ats == "greenhouse" else ASHBY_API).format(slug=slug)
    with _write_lock, HITS_PATH.open("a", newline="") as f:
        csv.writer(f).writerow([
            name, ats, slug, jobs,
            row.get("company_url", ""),
            row.get("company_linkedin_url", ""),
            row.get("company_country_code", ""),
            row.get("total_jobs_count", ""),
            row.get("company_industry", ""),
            sample,
        ])


def append_miss(name: str, row: dict, tried: list[tuple[str, str, int]]) -> None:
    tried_str = "; ".join(f"{ats}/{slug}({code})" for ats, slug, code in tried)
    with _write_lock, MISSES_PATH.open("a", newline="") as f:
        csv.writer(f).writerow([
            name,
            row.get("company_url", ""),
            row.get("company_linkedin_url", ""),
            row.get("company_country_code", ""),
            row.get("total_jobs_count", ""),
            row.get("company_industry", ""),
            tried_str,
        ])


def append_done(name: str) -> None:
    with _write_lock, DONE_PATH.open("a") as f:
        f.write(name + "\n")


def process_company(row: dict, existing_slugs: set[str]) -> str:
    """
    Returns one of: "hit_gh", "hit_ashby", "miss", "skip_known", "skip_empty".
    Thread-safe writes to hits/misses/done files.
    """
    name = (row.get("company_name") or "").strip()
    if not name:
        return "skip_empty"
    url = row.get("company_url") or None
    linkedin = row.get("company_linkedin_url") or None
    cands = generate_candidates(name, url, linkedin)

    known_hit = next((c for c in cands if c in existing_slugs), None)
    if known_hit:
        append_done(name)
        return "skip_known"

    tried: list[tuple[str, str, int]] = []
    resolved: Optional[tuple[str, str, int]] = None
    for cand in cands:
        status, jobs = probe_greenhouse(cand)
        tried.append(("gh", cand, status))
        if status == 200 and (jobs or 0) > 0:
            resolved = ("greenhouse", cand, jobs or 0)
            break
        time.sleep(REQUEST_DELAY_SEC)
        status, jobs = probe_ashby(cand)
        tried.append(("ashby", cand, status))
        if status == 200 and (jobs or 0) > 0:
            resolved = ("ashby", cand, jobs or 0)
            break
        time.sleep(REQUEST_DELAY_SEC)

    if resolved:
        ats, slug, jobs = resolved
        append_hit(name, ats, slug, jobs, row)
        append_done(name)
        return "hit_gh" if ats == "greenhouse" else "hit_ashby"
    append_miss(name, row, tried)
    append_done(name)
    return "miss"


def iter_csv(path: Path) -> Iterator[dict]:
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: derive_5k_companies.py <csv> [--limit N]", file=sys.stderr)
        return 2
    input_path = Path(sys.argv[1])
    limit = None
    for arg in sys.argv[2:]:
        if arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ensure_csv(HITS_PATH, HITS_COLUMNS)
    ensure_csv(MISSES_PATH, MISSES_COLUMNS)

    existing_slugs = load_existing_slugs()
    done = load_done_companies()
    print(f"Existing DB slugs (will skip): {len(existing_slugs)}", flush=True)
    print(f"Already-processed companies (resuming): {len(done)}", flush=True)

    counters = {"hit_gh": 0, "hit_ashby": 0, "miss": 0,
                "skip_known": 0, "skip_empty": 0}
    n_total = 0
    n_done_this_run = 0
    n_skipped_already = 0
    start = time.time()

    pending: list[dict] = []
    for row in iter_csv(input_path):
        n_total += 1
        if limit and n_total > limit:
            break
        name = (row.get("company_name") or "").strip()
        if not name:
            continue
        if name in done:
            n_skipped_already += 1
            continue
        pending.append(row)

    print(f"Companies to process this run: {len(pending)}", flush=True)

    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as ex:
        futures = {ex.submit(process_company, r, existing_slugs): r for r in pending}
        for fut in as_completed(futures):
            outcome = fut.result()
            counters[outcome] = counters.get(outcome, 0) + 1
            n_done_this_run += 1
            if n_done_this_run % 50 == 0:
                elapsed = time.time() - start
                rate = n_done_this_run / max(elapsed, 1)
                remaining = (len(pending) - n_done_this_run) / max(rate, 0.01)
                line = (f"[{time.strftime('%H:%M:%S')}] "
                        f"processed={n_done_this_run}/{len(pending)} "
                        f"gh={counters['hit_gh']} ashby={counters['hit_ashby']} "
                        f"miss={counters['miss']} skip_known={counters['skip_known']} "
                        f"rate={rate:.2f}/s eta_min={remaining/60:.1f}")
                with _write_lock, PROGRESS_PATH.open("a") as plog:
                    plog.write(line + "\n")
                print(line, flush=True)

    n_hit_gh = counters["hit_gh"]
    n_hit_ashby = counters["hit_ashby"]
    n_miss = counters["miss"]
    n_skipped_known = counters["skip_known"]

    elapsed = time.time() - start
    print(flush=True)
    print(f"=== Done ===", flush=True)
    print(f"  Rows seen:           {n_total}", flush=True)
    print(f"  Skipped (resumed):   {n_skipped_already}", flush=True)
    print(f"  Skipped (known DB):  {n_skipped_known}", flush=True)
    print(f"  Greenhouse hits:     {n_hit_gh}", flush=True)
    print(f"  Ashby hits:          {n_hit_ashby}", flush=True)
    print(f"  Misses:              {n_miss}", flush=True)
    print(f"  Elapsed:             {elapsed/60:.1f} min", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
