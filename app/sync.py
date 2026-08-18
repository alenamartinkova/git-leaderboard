import asyncio
import fnmatch
import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import OrgConfig, settings, token_env_key
from app.db import SessionLocal
from app.github import GitHubClient
from app.models import Contributor, Repo, SyncRun, WeeklyStat

logger = logging.getLogger(__name__)

# Single in-process lock — prevents the scheduler and a manual click from running
# two syncs against the same DB at once.
_sync_lock = asyncio.Lock()


def is_syncing() -> bool:
    return _sync_lock.locked()


def _week_start(d: date) -> date:
    """GitHub buckets weeks at Sunday 00:00 UTC."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


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


def _upsert_repo(db: Session, repo: dict, branch_count: int | None = None) -> Repo:
    """Insert/refresh repo metadata. Does NOT touch last_synced_at or
    history_synced_from — those are stamped only after stats actually land.

    ``branch_count=None`` means "we didn't count branches this time" (metadata-only
    listing), so an already stored count is left alone instead of being zeroed.
    """
    values = {
        "github_id": repo["id"],
        "name": repo["name"],
        "full_name": repo["full_name"],
        "org": (repo.get("owner") or {}).get("login") or repo["full_name"].split("/", 1)[0],
        "private": repo.get("private", False),
        "archived": repo.get("archived", False),
        "fork": repo.get("fork", False),
    }
    updates = dict(values)
    updates.pop("github_id")
    if branch_count is None:
        values["branch_count"] = 0          # len pre nový riadok
    else:
        values["branch_count"] = branch_count
        updates["branch_count"] = branch_count

    stmt = (
        pg_insert(Repo)
        .values(**values)
        .on_conflict_do_update(index_elements=[Repo.github_id], set_=updates)
        .returning(Repo.id)
    )
    rid = db.execute(stmt).scalar_one()
    return db.get(Repo, rid)


def _replace_weekly_stats(
    db: Session,
    repo_id: int,
    rows: list[dict],
    from_week: date | None,
) -> None:
    """Swap the repo's weekly stats for freshly computed ones.

    Delete-then-insert (instead of a blind upsert) because an incremental run
    re-reads a whole window: a force-push or a dropped branch has to be able to
    *lower* a week's numbers, and a partial re-read must never be added on top of
    what's already stored. ``from_week`` scopes the swap to the re-read window;
    None means the whole repo was re-read.
    """
    stmt = delete(WeeklyStat).where(WeeklyStat.repo_id == repo_id)
    if from_week is not None:
        stmt = stmt.where(WeeklyStat.week_start >= from_week)
    db.execute(stmt)

    if not rows:
        return

    insert_stmt = pg_insert(WeeklyStat)
    db.execute(
        insert_stmt.on_conflict_do_update(
            constraint="uq_weekly_stat",
            set_={
                "additions": insert_stmt.excluded.additions,
                "deletions": insert_stmt.excluded.deletions,
                "commits": insert_stmt.excluded.commits,
                "changed_files": insert_stmt.excluded.changed_files,
            },
        ),
        rows,
    )


def _can_skip_repo(db: Session, repo_data: dict) -> bool:
    """True if the repo has full history and hasn't been pushed to since last sync."""
    pushed_at_str = repo_data.get("pushed_at")
    if not pushed_at_str:
        return False
    existing = db.scalar(select(Repo).where(Repo.github_id == repo_data["id"]))
    if not existing or not existing.last_synced_at or not existing.history_synced_from:
        return False
    pushed_at = datetime.fromisoformat(pushed_at_str.replace("Z", "+00:00"))
    return pushed_at <= existing.last_synced_at


def _backfill_start(repo_data: dict) -> date:
    """How far back a full walk reaches: repo creation, or the configured cap."""
    if settings.sync_history_days > 0:
        return _week_start((datetime.now(UTC) - timedelta(days=settings.sync_history_days)).date())
    created = repo_data.get("created_at")
    if created:
        try:
            return _week_start(date.fromisoformat(created[:10]))
        except ValueError:
            pass
    return _week_start(datetime.now(UTC).date())


async def _sync_one(db: Session, gh: GitHubClient, repo_data: dict, full: bool = False) -> bool:
    """Sync a single repo's stats into DB. Returns True if data was written.

    A repo without ``history_synced_from`` is backfilled from its first commit, so
    every contributor has a complete timeline. Afterwards each run only re-reads
    the last ``sync_overlap_days``.
    """
    owner = repo_data["owner"]["login"]
    name = repo_data["name"]

    existing = db.scalar(select(Repo).where(Repo.github_id == repo_data["id"]))
    prev_synced_at = existing.last_synced_at if existing else None
    do_full = full or existing is None or existing.history_synced_from is None

    branches = await gh.list_branches(owner, name)
    repo = _upsert_repo(db, repo_data, branch_count=len(branches))
    db.commit()

    if do_full:
        since_dt = None
        if settings.sync_history_days > 0:
            since_dt = datetime.now(UTC) - timedelta(days=settings.sync_history_days)
        from_week: date | None = None if since_dt is None else _week_start(since_dt.date())
    else:
        base = prev_synced_at or datetime.now(UTC)
        since_dt = base - timedelta(days=settings.sync_overlap_days)
        from_week = _week_start(since_dt.date())

    logger.info(
        "syncing %s (%s)", repo_data["full_name"],
        "full backfill" if do_full else f"incremental from {from_week}",
    )

    stats = await gh.contributor_stats(owner, name, since_dt)

    now = datetime.now(UTC)
    if not stats:
        # Empty repo / no attributable commits in the window — nothing to write,
        # but the walk itself succeeded, so record the coverage.
        repo.last_synced_at = now
        if do_full:
            repo.history_synced_from = _backfill_start(repo_data)
        db.commit()
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
            week_start = date.fromtimestamp(week["w"])
            if from_week is not None and week_start < from_week:
                # Outside the re-read window — those weeks keep their stored value.
                continue
            key = (author["id"], week_start)
            bucket = agg.get(key)
            if bucket is None:
                bucket = {"author": author, "a": 0, "d": 0, "c": 0, "f": 0}
                agg[key] = bucket
            bucket["a"] += a
            bucket["d"] += d
            bucket["c"] += c
            bucket["f"] += f

    contributor_id_by_github_id: dict[int, int] = {}
    rows: list[dict] = []
    for (gh_id, week_start), v in agg.items():
        cid = contributor_id_by_github_id.get(gh_id)
        if cid is None:
            cid = _upsert_contributor(db, v["author"]).id
            contributor_id_by_github_id[gh_id] = cid
        rows.append(
            {
                "contributor_id": cid,
                "repo_id": repo.id,
                "week_start": week_start,
                "additions": v["a"],
                "deletions": v["d"],
                "commits": v["c"],
                "changed_files": v["f"],
            }
        )

    _replace_weekly_stats(db, repo.id, rows, from_week)
    repo.last_synced_at = now
    if do_full:
        repo.history_synced_from = _backfill_start(repo_data)
    db.commit()
    return bool(rows)


def _org_targets(repos: list[dict]) -> list[dict]:
    """Repos an org sync would touch: not archived, not excluded by pattern."""
    patterns = settings.exclude_patterns
    targets: list[dict] = []
    for repo_data in repos:
        if repo_data.get("archived"):
            continue
        if any(fnmatch.fnmatchcase(repo_data["name"], p) for p in patterns):
            logger.info("skipping %s (matches exclude pattern)", repo_data["name"])
            continue
        targets.append(repo_data)
    return targets


def _resolve_orgs(org: str | None) -> list[OrgConfig]:
    """Organizácie na spracovanie — jedna konkrétna, alebo všetky nakonfigurované."""
    if org:
        cfg = settings.org(org)
        if cfg is None:
            raise RuntimeError(f"organization {org!r} is not configured (GITHUB_ORGS)")
        targets = [cfg]
    else:
        targets = settings.orgs
    if not targets:
        raise RuntimeError("no organization configured — set GITHUB_ORGS (or GITHUB_ORG) in .env")
    missing = [c.name for c in targets if not c.token]
    if missing:
        raise RuntimeError(
            "missing token for " + ", ".join(missing)
            + " — set " + ", ".join(token_env_key(n) for n in missing) + " (or GITHUB_TOKEN)"
        )
    return targets


async def run_sync(full: bool = False, org: str | None = None) -> SyncRun:
    """Sync every repo in every configured org (or just ``org`` if given).

    ``full=True`` forces a complete re-read of every repo's history (use after
    changing the metric definitions). Otherwise repos that already have full
    history only get the recent window topped up.
    """
    async with _sync_lock:
        db = SessionLocal()
        run = SyncRun(scope="org", mode="full" if full else "incremental")
        db.add(run)
        db.commit()
        db.refresh(run)

        repos_synced = 0
        try:
            org_configs = _resolve_orgs(org)
            # Zoznamy repozitárov naprieč orgami načítame najprv, aby sa dal
            # ukázať zmysluplný progres (x/y) za celý beh, nie za každý org zvlášť.
            per_org: list[tuple[OrgConfig, list[dict]]] = []
            for cfg in org_configs:
                async with GitHubClient(cfg.token, settings.sync_page_size) as gh:
                    repos = await gh.list_org_repos(cfg.name)
                logger.info("found %d repos in %s", len(repos), cfg.name)
                per_org.append((cfg, _org_targets(repos)))

            run.total_repos = sum(len(targets) for _, targets in per_org)
            db.commit()

            idx = 0
            failed: list[str] = []
            for cfg, targets in per_org:
                async with GitHubClient(cfg.token, settings.sync_page_size) as gh:
                    for repo_data in targets:
                        idx += 1
                        run.current_index = idx
                        run.current_repo = repo_data["full_name"]
                        db.commit()

                        try:
                            if not full and _can_skip_repo(db, repo_data):
                                logger.info("skipping %s (no pushes since last sync)", repo_data["full_name"])
                            elif await _sync_one(db, gh, repo_data, full=full):
                                repos_synced += 1
                        except Exception as exc:
                            # Jedno rozbité repo nesmie zhodiť celý nočný beh —
                            # zvyšok sa dosynchronizuje a chyby zhrnieme na konci.
                            db.rollback()
                            logger.exception("sync failed for %s", repo_data["full_name"])
                            failed.append(f"{repo_data['full_name']}: {exc}")
                        run.repos_synced = repos_synced
                        db.commit()

            run.finished_at = datetime.now(UTC)
            run.current_repo = None
            if failed:
                summary = f"{len(failed)} repo(s) failed — " + "; ".join(failed)
                run.error = summary[:2000]
            db.commit()
            logger.info(
                "sync finished: %d repos across %d org(s), %d failed",
                repos_synced, len(org_configs), len(failed),
            )
        except Exception as exc:
            logger.exception("sync failed")
            run.error = str(exc)[:2000]
            run.finished_at = datetime.now(UTC)
            db.commit()
            raise
        finally:
            db.close()

        return run


async def run_sync_repo_list(org: str | None = None) -> SyncRun:
    """Fetch just the repo lists — no commit history.

    Cheap (one REST page per 100 repos) and finishes in seconds, so the repos
    page fills up right away and each repo can then be synced individually.
    """
    async with _sync_lock:
        db = SessionLocal()
        run = SyncRun(scope="list", mode="metadata")
        db.add(run)
        db.commit()
        db.refresh(run)

        try:
            org_configs = _resolve_orgs(org)
            total = 0
            for cfg in org_configs:
                async with GitHubClient(cfg.token, settings.sync_page_size) as gh:
                    repos = await gh.list_org_repos(cfg.name)
                targets = _org_targets(repos)
                for repo_data in targets:
                    _upsert_repo(db, repo_data)
                    total += 1
                    run.current_index = total
                    run.repos_synced = total
                    run.current_repo = repo_data["full_name"]
                run.total_repos = total
                db.commit()
                logger.info("repo list synced: %d repos in %s", len(targets), cfg.name)

            run.finished_at = datetime.now(UTC)
            run.current_repo = None
            db.commit()
        except Exception as exc:
            logger.exception("repo list sync failed")
            run.error = str(exc)[:2000]
            run.finished_at = datetime.now(UTC)
            db.commit()
            raise
        finally:
            db.close()

        return run


async def run_sync_repo(full_name: str, full: bool = False) -> SyncRun:
    """Sync a single repo by full_name (owner/repo)."""
    if "/" not in full_name:
        raise ValueError("repo full_name must be in the form 'owner/repo'")
    owner, name = full_name.split("/", 1)

    # Token sa vyberá podľa vlastníka repa — každý org môže mať vlastný.
    token = settings.token_for(owner)
    if not token:
        raise RuntimeError(
            f"no token for {owner} — set {token_env_key(owner)} (or GITHUB_TOKEN) in .env"
        )

    async with _sync_lock:
        db = SessionLocal()
        run = SyncRun(
            scope="repo",
            mode="full" if full else "incremental",
            total_repos=1,
            current_index=1,
            current_repo=full_name,
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        try:
            async with GitHubClient(token, settings.sync_page_size) as gh:
                repo_data = await gh.get_repo(owner, name)
                if await _sync_one(db, gh, repo_data, full=full):
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
