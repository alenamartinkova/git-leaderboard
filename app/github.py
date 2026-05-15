import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# Pull all the fields we need from each commit in one paginated GraphQL query.
# Skips merge commits because their additions/deletions double-count branch
# contents and the REST /stats endpoint we replaced ignored them too.
_COMMIT_HISTORY_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
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

    async def contributor_stats(self, owner: str, repo: str) -> list[dict[str, Any]] | None:
        """Aggregate per-contributor weekly stats via GraphQL commit history.

        Returns the same shape the old REST /stats/contributors endpoint did:
            [{"author": {id, login, avatar_url, html_url}, "weeks": [{"w": ts, "a", "d", "c", "f"}]}]
        so the caller doesn't need to know we switched APIs. Returns None if the
        repo is empty / has no default branch.
        """
        # author_id -> {"author": {...}, "weeks": {ts -> [a, d, c, f]}}
        acc: dict[int, dict[str, Any]] = {}
        cursor: str | None = None
        page_count = 0

        while True:
            data = await self._graphql(_COMMIT_HISTORY_QUERY, {"owner": owner, "name": repo, "cursor": cursor})
            ref = (data.get("repository") or {}).get("defaultBranchRef")
            if not ref or not ref.get("target"):
                return None

            history = ref["target"]["history"]
            for node in history["nodes"]:
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

            page_count += 1
            if not history["pageInfo"]["hasNextPage"]:
                break
            cursor = history["pageInfo"]["endCursor"]

        logger.info("graphql: %s/%s aggregated %d contributors across %d pages", owner, repo, len(acc), page_count)

        return [
            {
                "author": entry["author"],
                "weeks": [{"w": ts, "a": a, "d": d, "c": c, "f": f} for ts, (a, d, c, f) in sorted(entry["weeks"].items())],
            }
            for entry in acc.values()
        ]
