import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

_BRANCHES_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    refs(refPrefix: "refs/heads/", first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        target {
          ... on Commit {
            oid
            committedDate
          }
        }
      }
    }
  }
}
"""

# Pull commit history for a single branch, filtered to the recent window.
# Skips merge commits in code because their additions/deletions double-count branch
# contents and the REST /stats endpoint we replaced ignored them too.
_BRANCH_HISTORY_QUERY = """
query($owner: String!, $name: String!, $branch: String!, $since: GitTimestamp!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $branch) {
      target {
        ... on Commit {
          history(first: 100, after: $cursor, since: $since) {
            pageInfo { hasNextPage endCursor }
            nodes {
              oid
              committedDate
              additions
              deletions
              changedFilesIfAvailable
              parents(first: 2) { totalCount }
              author {
                user {
                  databaseId
                  login
                  avatarUrl
                  url
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _week_start_ts(committed_at: datetime) -> int:
    """Bucket a commit into the Sunday-00:00-UTC week (matches GitHub /stats output)."""
    d = committed_at.astimezone(UTC).date()
    days_back = (d.weekday() + 1) % 7  # Mon=0..Sun=6 -> back to previous Sunday
    sunday = d - timedelta(days=days_back)
    return int(datetime(sunday.year, sunday.month, sunday.day, tzinfo=UTC).timestamp())


class GitHubClient:
    def __init__(self, token: str):
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "git-leaderboard",
            },
            timeout=httpx.Timeout(60.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def list_org_repos(self, org: str) -> list[dict[str, Any]]:
        repos: list[dict[str, Any]] = []
        page = 1
        while True:
            r = await self._client.get(
                f"/orgs/{org}/repos",
                params={"per_page": 100, "page": page, "type": "all"},
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            repos.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return repos

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        r = await self._client.get(f"/repos/{owner}/{repo}")
        r.raise_for_status()
        return r.json()

    async def list_branches(self, owner: str, repo: str) -> list[dict[str, Any]]:
        branches: list[dict[str, Any]] = []
        page = 1
        while True:
            r = await self._client.get(
                f"/repos/{owner}/{repo}/branches",
                params={"per_page": 100, "page": page},
            )
            if r.status_code == 404:
                return []
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            branches.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return branches

    async def _graphql(self, query: str, variables: dict) -> dict:
        r = await self._client.post("/graphql", json={"query": query, "variables": variables})
        r.raise_for_status()
        payload = r.json()
        if "errors" in payload:
            raise RuntimeError(f"GraphQL errors: {payload['errors']}")
        return payload["data"]

    async def _list_branches(self, owner: str, repo: str) -> tuple[list[dict[str, Any]], str | None]:
        """Returns ([{name, tip_oid, tip_date}], default branch name or None)."""
        branches: list[dict[str, Any]] = []
        default: str | None = None
        cursor: str | None = None
        while True:
            data = await self._graphql(_BRANCHES_QUERY, {"owner": owner, "name": repo, "cursor": cursor})
            repo_data = data.get("repository") or {}
            if default is None:
                default = (repo_data.get("defaultBranchRef") or {}).get("name")
            refs = repo_data.get("refs")
            if not refs:
                return branches, default
            for node in refs["nodes"]:
                target = node.get("target") or {}
                branches.append({
                    "name": node["name"],
                    "tip_oid": target.get("oid"),
                    "tip_date": target.get("committedDate"),
                })
            if not refs["pageInfo"]["hasNextPage"]:
                break
            cursor = refs["pageInfo"]["endCursor"]
        return branches, default

    async def contributor_stats(self, owner: str, repo: str) -> list[dict[str, Any]] | None:
        """Aggregate per-contributor weekly stats via GraphQL commit history.

        Walks every branch and dedupes by commit OID so feature-branch work counts
        even before it's merged, without double-counting after merge. History is
        limited to the last ``sync_history_days`` to keep GraphQL calls bounded.

        Returns the same shape the old REST /stats/contributors endpoint did:
            [{"author": {id, login, avatar_url, html_url}, "weeks": [{"w": ts, "a", "d", "c", "f"}]}]
        Returns None if the repo is empty / has no branches.
        """
        branches, default = await self._list_branches(owner, repo)
        if not branches:
            return None

        since_dt = datetime.now(UTC) - timedelta(days=settings.sync_history_days)
        since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Drop branches whose tip is older than the window — they can't contribute
        # any new commits.
        active: list[dict[str, Any]] = []
        for b in branches:
            tip_date = b.get("tip_date")
            if tip_date:
                try:
                    if datetime.fromisoformat(tip_date.replace("Z", "+00:00")) < since_dt:
                        continue
                except ValueError:
                    pass
            active.append(b)

        # Walk default branch first so other branches can short-circuit pagination
        # (and tip-OID skip) as soon as they hit ancestry already covered by main.
        if default:
            active.sort(key=lambda b: 0 if b["name"] == default else 1)

        skipped_stale = len(branches) - len(active)

        # author_id -> {"author": {...}, "weeks": {ts -> [a, d, c, f]}}
        acc: dict[int, dict[str, Any]] = {}
        seen_oids: set[str] = set()
        page_count = 0
        skipped_merged = 0

        for b in active:
            branch = b["name"]
            tip_oid = b.get("tip_oid")
            # Tip already covered by an earlier branch (typically main) -> fully merged.
            if tip_oid and tip_oid in seen_oids:
                skipped_merged += 1
                continue
            qualified = f"refs/heads/{branch}"
            cursor: str | None = None
            while True:
                data = await self._graphql(
                    _BRANCH_HISTORY_QUERY,
                    {"owner": owner, "name": repo, "branch": qualified, "since": since_iso, "cursor": cursor},
                )
                ref = (data.get("repository") or {}).get("ref")
                if not ref or not ref.get("target"):
                    break

                history = ref["target"]["history"]
                page_count += 1
                new_in_page = 0
                for node in history["nodes"]:
                    oid = node["oid"]
                    if oid in seen_oids:
                        continue
                    seen_oids.add(oid)
                    new_in_page += 1

                    # Skip merge commits — their additions/deletions are noisy.
                    if (node.get("parents") or {}).get("totalCount", 0) > 1:
                        continue
                    user = (node.get("author") or {}).get("user")
                    if not user:  # ghost author (email not linked to a GitHub account)
                        continue

                    committed = datetime.fromisoformat(node["committedDate"].replace("Z", "+00:00"))
                    ts = _week_start_ts(committed)

                    entry = acc.setdefault(
                        user["databaseId"],
                        {
                            "author": {
                                "id": user["databaseId"],
                                "login": user["login"],
                                "avatar_url": user.get("avatarUrl"),
                                "html_url": user.get("url"),
                            },
                            "weeks": {},
                        },
                    )
                    bucket = entry["weeks"].setdefault(ts, [0, 0, 0, 0])
                    bucket[0] += node.get("additions", 0) or 0
                    bucket[1] += node.get("deletions", 0) or 0
                    bucket[2] += 1
                    bucket[3] += node.get("changedFilesIfAvailable", 0) or 0

                # If the whole page was already-seen commits, we've walked into
                # ancestry that another branch already covered — the rest are
                # all ancestors too, so we can stop paginating this branch.
                if history["nodes"] and new_in_page == 0:
                    break
                if not history["pageInfo"]["hasNextPage"]:
                    break
                cursor = history["pageInfo"]["endCursor"]

        logger.info(
            "graphql: %s/%s aggregated %d contributors from %d/%d branches across %d pages "
            "(%d unique commits, skipped %d stale, %d merged)",
            owner, repo, len(acc), len(active) - skipped_merged, len(branches),
            page_count, len(seen_oids), skipped_stale, skipped_merged,
        )

        if not acc:
            return None

        return [
            {
                "author": entry["author"],
                "weeks": [{"w": ts, "a": a, "d": d, "c": c, "f": f} for ts, (a, d, c, f) in sorted(entry["weeks"].items())],
            }
            for entry in acc.values()
        ]
