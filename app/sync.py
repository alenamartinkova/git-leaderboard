import asyncio
import fnmatch
import logging
from datetime import UTC, date, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.github import GitHubClient
from app.models import Contributor, Repo, SyncRun, WeeklyStat

logger = logging.getLogger(__name__)


def _upsert_contributor(db: Session, author: dict) -> Contributor:
    stmt = (
        pg_insert(Contributor)
        .values(
            github_id=author["id"],
            login=author["login"],
            avatar_url=author.get("avatar_url"),
            html_url=author.get("html_url"),
        )
        .on_conflict_do_update(
            index_elements=[Contributor.github_id],
            set_={"login": author["login"], "avatar_url": author.get("avatar_url"), "html_url": author.get("html_url")},
        )
        .returning(Contributor.id)
    )
    cid = db.execute(stmt).scalar_one()
    return db.get(Contributor, cid)


def _upsert_repo(db: Session, repo: dict) -> Repo:
    stmt = (
        pg_insert(Repo)
        .values(
            github_id=repo["id"],
            name=repo["name"],
            full_name=repo["full_name"],
            private=repo.get("private", False),
            archived=repo.get("archived", False),
            fork=repo.get("fork", False),
            last_synced_at=datetime.now(UTC),
        )
        .on_conflict_do_update(
            index_elements=[Repo.github_id],
            set_={
                "name": repo["name"],
                "full_name": repo["full_name"],
                "private": repo.get("private", False),
                "archived": repo.get("archived", False),
                "fork": repo.get("fork", False),
                "last_synced_at": datetime.now(UTC),
            },
        )
        .returning(Repo.id)
    )
    rid = db.execute(stmt).scalar_one()
    return db.get(Repo, rid)


def _upsert_weekly_stat(db: Session, contributor_id: int, repo_id: int, week_start: date, a: int, d: int, c: int) -> None:
    stmt = (
        pg_insert(WeeklyStat)
        .values(
            contributor_id=contributor_id,
            repo_id=repo_id,
            week_start=week_start,
            additions=a,
            deletions=d,
            commits=c,
        )
        .on_conflict_do_update(
            constraint="uq_weekly_stat",
            set_={"additions": a, "deletions": d, "commits": c},
        )
    )
    db.execute(stmt)


async def run_sync() -> SyncRun:
    if not settings.github_token or not settings.github_org:
        raise RuntimeError("GITHUB_TOKEN and GITHUB_ORG must be set in .env")

    db = SessionLocal()
    run = SyncRun()
    db.add(run)
    db.commit()
    db.refresh(run)

    repos_synced = 0
    try:
        async with GitHubClient(settings.github_token) as gh:
            repos = await gh.list_org_repos(settings.github_org)
            logger.info("found %d repos in %s", len(repos), settings.github_org)

            patterns = settings.exclude_patterns
            targets: list[dict] = []
            for repo_data in repos:
                if repo_data.get("archived"):
                    continue
                name = repo_data["name"]
                if any(fnmatch.fnmatchcase(name, p) for p in patterns):
                    logger.info("skipping %s (matches exclude pattern)", name)
                    continue
                targets.append(repo_data)

            # Pass 1: prime GitHub's stats cache for every repo so it computes them
            # in parallel on the server side. First-ever sync otherwise hits 202s
            # that don't resolve within our per-repo retry budget.
            not_ready = 0
            for repo_data in targets:
                ready = await gh.prime_contributor_stats(repo_data["owner"]["login"], repo_data["name"])
                if not ready:
                    not_ready += 1
            if not_ready:
                logger.info("priming %d repos, waiting 30s for GitHub to compute stats", not_ready)
                await asyncio.sleep(30)

            # Pass 2: actually fetch the stats.
            for repo_data in targets:
                repo = _upsert_repo(db, repo_data)
                db.commit()

                owner = repo_data["owner"]["login"]
                stats = await gh.contributor_stats(owner, repo_data["name"])
                if not stats:
                    continue

                for entry in stats:
                    author = entry.get("author")
                    if not author:  # ghost contributor
                        continue
                    contributor = _upsert_contributor(db, author)
                    for week in entry.get("weeks", []):
                        a, d, c = week.get("a", 0), week.get("d", 0), week.get("c", 0)
                        if a == 0 and d == 0 and c == 0:
                            continue
                        week_start = date.fromtimestamp(week["w"])
                        _upsert_weekly_stat(db, contributor.id, repo.id, week_start, a, d, c)
                    db.commit()
                repos_synced += 1

        run.finished_at = datetime.now(UTC)
        run.repos_synced = repos_synced
        db.commit()
        logger.info("sync finished: %d repos", repos_synced)
    except Exception as exc:
        logger.exception("sync failed")
        run.error = str(exc)[:2000]
        run.finished_at = datetime.now(UTC)
        db.commit()
        raise
    finally:
        db.close()

    return run
