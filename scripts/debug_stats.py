"""Standalone diagnostic for GitHub /stats/contributors.

Usage:
    docker compose exec app python scripts/debug_stats.py owner/repo

Or locally if you have python + httpx + python-dotenv:
    python scripts/debug_stats.py owner/repo

Reads GITHUB_TOKEN from .env. Prints exactly what GitHub returns at every step
so we can tell whether it's a GitHub issue or our code.
"""
from __future__ import annotations

import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
API = "https://api.github.com"

if not TOKEN:
    sys.exit("GITHUB_TOKEN not set in .env")

if len(sys.argv) != 2 or "/" not in sys.argv[1]:
    sys.exit("usage: debug_stats.py owner/repo")

owner, repo = sys.argv[1].split("/", 1)

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "git-leaderboard-debug",
}

client = httpx.Client(base_url=API, headers=headers, timeout=30.0)


def banner(text: str) -> None:
    print(f"\n{'=' * 60}\n{text}\n{'=' * 60}")


def rate_limit_summary() -> None:
    r = client.get("/rate_limit")
    if r.status_code != 200:
        print(f"rate_limit endpoint returned {r.status_code}")
        return
    core = r.json()["resources"]["core"]
    reset_in = max(0, core["reset"] - int(time.time()))
    print(f"rate limit: {core['remaining']}/{core['limit']} (resets in {reset_in}s)")


banner(f"1) Repo metadata: GET /repos/{owner}/{repo}")
r = client.get(f"/repos/{owner}/{repo}")
print(f"status: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    print(f"  full_name:      {data['full_name']}")
    print(f"  default_branch: {data['default_branch']}")
    print(f"  private:        {data['private']}")
    print(f"  archived:       {data['archived']}")
    print(f"  fork:           {data['fork']}")
    print(f"  size (KB):      {data['size']}")
    print(f"  pushed_at:      {data['pushed_at']}")
    default_branch = data["default_branch"]
else:
    print(f"body: {r.text[:500]}")
    sys.exit(1)


banner(f"2) Commits on default branch ({default_branch}): GET /repos/{owner}/{repo}/commits?sha={default_branch}&per_page=10")
r = client.get(f"/repos/{owner}/{repo}/commits", params={"sha": default_branch, "per_page": 10})
print(f"status: {r.status_code}")
if r.status_code == 200:
    commits = r.json()
    print(f"  commits on '{default_branch}': {len(commits)} (max 10 shown)")
    for c in commits[:10]:
        author = (c.get("author") or {}).get("login") or "<ghost>"
        print(f"    {c['sha'][:7]}  {author:20s}  {c['commit']['message'].splitlines()[0][:60]}")
    if len(commits) == 0:
        print("  ⚠ default branch has 0 commits — /stats/contributors will return 204 (empty).")
else:
    print(f"body: {r.text[:500]}")


banner(f"3) Contributors (cheap endpoint): GET /repos/{owner}/{repo}/contributors")
r = client.get(f"/repos/{owner}/{repo}/contributors")
print(f"status: {r.status_code}")
if r.status_code == 200:
    contribs = r.json()
    print(f"  contributors: {len(contribs)}")
    for c in contribs[:10]:
        print(f"    {c.get('login'):20s}  {c.get('contributions')} commits")
elif r.status_code == 204:
    print("  204 No Content — repo has no commits / no contributors")
else:
    print(f"body: {r.text[:500]}")


banner(f"4) Stats/contributors with polling: GET /repos/{owner}/{repo}/stats/contributors")
url = f"/repos/{owner}/{repo}/stats/contributors"
start = time.time()
got_200 = False
for attempt in range(1, 16):
    r = client.get(url)
    elapsed = time.time() - start
    print(f"  attempt {attempt:2d}  t+{elapsed:5.1f}s  status={r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"    ✓ got data: {len(data)} contributor entries")
        for entry in data[:5]:
            author = entry.get("author") or {}
            weeks = entry.get("weeks", [])
            nonzero = [w for w in weeks if w.get("a") or w.get("d") or w.get("c")]
            print(f"      {author.get('login', '<ghost>'):20s}  total={entry.get('total')}  weeks_with_activity={len(nonzero)}/{len(weeks)}")
        got_200 = True
        break
    if r.status_code == 204:
        print("    204 No Content — GitHub says no data for this repo (likely empty default branch)")
        break
    if r.status_code == 202:
        time.sleep(5)
        continue
    print(f"    body: {r.text[:500]}")
    break

if not got_200 and r.status_code == 202:
    print(f"\n  ⚠ Still 202 after {attempt} attempts ({elapsed:.0f}s). GitHub is not computing the stats.")
    print("    This is a known GitHub issue for some repos. Workaround: fall back to enumerating commits.")


banner("5) Rate limit")
rate_limit_summary()

client.close()
