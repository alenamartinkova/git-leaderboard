import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


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
            timeout=httpx.Timeout(30.0),
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

    async def prime_contributor_stats(self, owner: str, repo: str) -> bool:
        """Trigger stats computation without waiting. Returns True if data is already
        cached and ready (200), False if GitHub started computing in the background (202)."""
        r = await self._client.get(f"/repos/{owner}/{repo}/stats/contributors")
        if r.status_code in (200, 204):
            return True
        if r.status_code == 202:
            return False
        r.raise_for_status()
        return False

    async def contributor_stats(
        self, owner: str, repo: str, *, max_retries: int = 10, retry_delay: float = 3.0
    ) -> list[dict[str, Any]] | None:
        """GET /repos/{owner}/{repo}/stats/contributors.

        GitHub returns 202 while it computes stats — we retry with backoff.
        Returns None for empty repos (204) or when stats never become ready.
        """
        url = f"/repos/{owner}/{repo}/stats/contributors"
        for attempt in range(max_retries):
            r = await self._client.get(url)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 204:
                return None
            if r.status_code == 202:
                logger.info("stats computing for %s/%s, retry %d/%d", owner, repo, attempt + 1, max_retries)
                await asyncio.sleep(retry_delay * (attempt + 1))
                continue
            r.raise_for_status()
        logger.warning("stats not ready after %d retries for %s/%s", max_retries, owner, repo)
        return None
