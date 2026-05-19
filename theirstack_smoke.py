"""
TheirStack API smoke test. Goal: confirm whether already-revealed companies
incur API credits on /v1/jobs/search, or whether the UI's "free for revealed
companies" rule extends to the API.

Strategy: one tiny request (limit=10), filter to US+greenhouse+ashby+30 days
(same filter used in the user's UI export). Print every response header and
inspect the first job to confirm response shape + URL field.

Run: python3 theirstack_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


ENDPOINT = "https://api.theirstack.com/v1/jobs/search"
ENV_FILE = Path(__file__).parent / ".env"


def load_env(path: Path) -> None:
    """Tiny .env loader (no external deps). Only sets keys not already in os.environ."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    load_env(ENV_FILE)
    key = os.environ.get("THEIRSTACK_API_KEY")
    if not key:
        print("THEIRSTACK_API_KEY not set in .env", file=sys.stderr)
        return 1

    body = {
        "include_total_results": False,
        "posted_at_max_age_days": 30,
        "company_country_code_or": ["US"],
        "company_technology_slug_or": ["greenhouse", "ashby"],
        "revealed_company_data": True,
        "blur_company_data": False,
        "page": 0,
        "limit": 10,
    }

    req = urllib.request.Request(
        ENDPOINT,
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )

    print(f"POST {ENDPOINT}")
    print(f"Body: {json.dumps(body)}")
    print()

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            headers = dict(resp.headers.items())
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} {e.reason}")
        try:
            err_body = e.read().decode("utf-8")
            print(err_body)
        except Exception:
            pass
        return 1

    print(f"HTTP {status}")
    print()
    print("=== Response headers ===")
    for k, v in sorted(headers.items()):
        print(f"  {k}: {v}")
    print()

    print("=== Response payload shape ===")
    if isinstance(payload, dict):
        print(f"  Top-level keys: {list(payload.keys())}")
        data = payload.get("data") or payload.get("jobs") or payload.get("results") or []
        print(f"  Job count in response: {len(data)}")
        if data:
            sample = data[0]
            print()
            print(f"  Sample job fields ({len(sample)} total):")
            for k in sorted(sample.keys()):
                v = sample[k]
                if isinstance(v, str) and len(v) > 90:
                    v = v[:90] + "…"
                print(f"    {k}: {v!r}")
            url_fields = [k for k in sample.keys() if "url" in k.lower()]
            print()
            print(f"  URL-typed fields: {url_fields}")
            for f in url_fields:
                print(f"    {f}: {sample.get(f)!r}")
    else:
        print(f"  Unexpected response type: {type(payload).__name__}")
        print(repr(payload)[:500])

    print()
    print("=== Verdict on credit charging ===")
    credit_headers = {k: v for k, v in headers.items()
                      if "credit" in k.lower() or "usage" in k.lower() or "quota" in k.lower()}
    if credit_headers:
        print(f"  Credit-related headers: {credit_headers}")
    else:
        print("  No explicit credit headers in response.")
    print("  -> Check the TheirStack dashboard for credit-balance change to confirm")
    print("     whether this request consumed credits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
