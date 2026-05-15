import asyncio
import fnmatch
import logging
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.github import GitHubClient
from app.models import Contributor, Repo, SyncRun, WeeklyStat

logger = logging.getLogger(__name__)

# Single in-process lock — prevents the scheduler and a manual click from running
# two syncs against the same DB at once.
_sync_lock = asyncio.Lock()


def is_syncing() -> bool:
    return _sync_lock.locked()


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


def _upsert_repo(db: Session, repo: dict, branch_count: int = 0) -> Repo:
    stmt = (
        pg_insert(Repo)
        .values(
            github_id=repo["id"],
            name=repo["name"],
            full_name=repo["full_name"],
            private=repo.get("private", False),
            archived=repo.get("archived", False),
            fork=repo.get("fork", False),
            branch_count=branch_count,
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
                "branch_count": branch_count,
                "last_synced_at": datetime.now(UTC),
            },
        )
        .returning(Repo.id)
    )
    rid = db.execute(stmt).scalar_one()
    return db.get(Repo, rid)


def _upsert_weekly_stat(
    db: Session,
    contributor_id: int,
    repo_id: int,
    week_start: date,
    a: int,
    d: int,
    c: int,
    f: int,
) -> None:
    stmt = (
        pg_insert(WeeklyStat)
        .values(
            contributor_id=contributor_id,
            repo_id=repo_id,
            week_start=week_start,
            additions=a,
            deletions=d,
            commits=c,
            changed_files=f,
        )
        .on_conflict_do_update(
            constraint="uq_weekly_stat",
            set_={
                "additions": a,
                "deletions": d,
                "commits": c,
                "changed_files": f,
            },
        )
    )
    db.execute(stmt)


def _can_skip_repo(db: Session, repo_data: dict) -> bool:
    """True if the repo hasn't been pushed to since its last successful sync."""
    pushed_at_str = repo_data.get("pushed_at")
    if not pushed_at_str:
        return False
    existing = db.scalar(select(Repo).where(Repo.github_id == repo_data["id"]))
    if not existing or not existing.last_synced_at:
        return False
    pushed_at = datetime.fromisoformat(pushed_at_str.replace("Z", "+00:00"))
    return pushed_at <= existing.last_synced_at


async def _sync_one(db: Session, gh: GitHubClient, repo_data: dict) -> bool:
    """Sync a single repo's stats into DB. Returns True if data was written."""
    owner = repo_data["owner"]["login"]
    name = repo_data["name"]

    branches = await gh.list_branches(owner, name)
    repo = _upsert_repo(db, repo_data, branch_count=len(branches))
    db.commit()

    stats = await gh.contributor_stats(owner, name)
    if not stats:
        return False

    # key: (github_user_id, week_start) -> {"author": {...}, "a", "d", "c", "f"}
    agg: dict[tuple[int, date], dict] = {}

    for entry in stats:
        author = entry.get("author")
        if not author:  # ghost contributor
            continue
        for week in entry.get("weeks", []):
            a, d, c, f = week.get("a", 0), week.get("d", 0), week.get("c", 0), week.get("f", 0)
            if a == 0 and d == 0 and c == 0:
                continue
            key = (author["id"], date.fromtimestamp(week["w"]))
            bucket = agg.get(key)
            if bucket is None:
                bucket = {"author": author, "a": 0, "d": 0, "c": 0, "f": 0}
                agg[key] = bucket
            bucket["a"] += a
            bucket["d"] += d
            bucket["c"] += c
            bucket["f"] += f

    if not agg:
        return False

    contributor_id_by_github_id: dict[int, int] = {}
    for (gh_id, week_start), v in agg.items():
        cid = contributor_id_by_github_id.get(gh_id)
        if cid is None:
            cid = _upsert_contributor(db, v["author"]).id
            contributor_id_by_github_id[gh_id] = cid
        _upsert_weekly_stat(
            db, cid, repo.id, week_start, v["a"], v["d"], v["c"], v["f"]
        )
    db.commit()
    return True


async def run_sync() -> SyncRun:
    if not settings.github_token or not settings.github_org:
        raise RuntimeError("GITHUB_TOKEN and GITHUB_ORG must be set in .env")

    async with _sync_lock:
        db = SessionLocal()
        run = SyncRun(scope="org")
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

                run.total_repos = len(targets)
                db.commit()

                for idx, repo_data in enumerate(targets, start=1):
                    run.current_index = idx
                    run.current_repo = repo_data["full_name"]
                    db.commit()

                    if _can_skip_repo(db, repo_data):
                        logger.info("skipping %s (no pushes since last sync)", repo_data["full_name"])
                    elif await _sync_one(db, gh, repo_data):
                        repos_synced += 1
                    run.repos_synced = repos_synced
                    db.commit()

            run.finished_at = datetime.now(UTC)
            run.current_repo = None
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


async def run_sync_repo(full_name: str) -> SyncRun:
    """Sync a single repo by full_name (owner/repo)."""
    if not settings.github_token:
        raise RuntimeError("GITHUB_TOKEN must be set in .env")
    if "/" not in full_name:
        raise ValueError("repo full_name must be in the form 'owner/repo'")
    owner, name = full_name.split("/", 1)

    async with _sync_lock:
        db = SessionLocal()
        run = SyncRun(scope="repo", total_repos=1, current_index=1, current_repo=full_name)
        db.add(run)
        db.commit()
        db.refresh(run)

        try:
            async with GitHubClient(settings.github_token) as gh:
                repo_data = await gh.get_repo(owner, name)
                if await _sync_one(db, gh, repo_data):
                    run.repos_synced = 1

            run.finished_at = datetime.now(UTC)
            run.current_repo = None
            db.commit()
            logger.info("sync finished for %s", full_name)
        except Exception as exc:
            logger.exception("repo sync failed for %s", full_name)
            run.error = str(exc)[:2000]
            run.finished_at = datetime.now(UTC)
            db.commit()
            raise
        finally:
            db.close()

        return run
