from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Contributor, Repo, SyncRun, WeeklyStat


@dataclass
class WeeklyTotals:
    week_start: date
    additions: int
    deletions: int
    commits: int
    changed_files: int


@dataclass
class RepoActivity:
    full_name: str
    additions: int
    deletions: int
    commits: int


@dataclass
class PeriodTotals:
    additions: int
    deletions: int
    commits: int
    changed_files: int
    contributors: int


@dataclass
class LeaderboardRow:
    login: str
    avatar_url: str | None
    html_url: str | None
    additions: int
    deletions: int
    commits: int
    changed_files: int


def _aggregate(db: Session, since: date | None, repo_full_name: str | None = None) -> list[LeaderboardRow]:
    stmt = (
        select(
            Contributor.login,
            Contributor.avatar_url,
            Contributor.html_url,
            func.coalesce(func.sum(WeeklyStat.additions), 0).label("additions"),
            func.coalesce(func.sum(WeeklyStat.deletions), 0).label("deletions"),
            func.coalesce(func.sum(WeeklyStat.commits), 0).label("commits"),
            func.coalesce(func.sum(WeeklyStat.changed_files), 0).label("changed_files"),
        )
        .join(WeeklyStat, WeeklyStat.contributor_id == Contributor.id)
        .group_by(Contributor.id)
        .order_by(func.sum(WeeklyStat.additions).desc())
    )
    if since is not None:
        stmt = stmt.where(WeeklyStat.week_start >= since)
    if repo_full_name:
        stmt = stmt.join(Repo, Repo.id == WeeklyStat.repo_id).where(Repo.full_name == repo_full_name)
    rows = db.execute(stmt).all()
    return [
        LeaderboardRow(
            login=r.login,
            avatar_url=r.avatar_url,
            html_url=r.html_url,
            additions=r.additions,
            deletions=r.deletions,
            commits=r.commits,
            changed_files=r.changed_files,
        )
        for r in rows
    ]


def _week_start(d: date) -> date:
    # GitHub buckets weeks at Sunday 00:00 UTC.
    return d - timedelta(days=(d.weekday() + 1) % 7)


def leaderboard(db: Session, period: str, repo_full_name: str | None = None) -> list[LeaderboardRow]:
    today = datetime.now(UTC).date()
    if period == "week":
        since = _week_start(today)
    elif period == "month":
        since = today - timedelta(days=30)
    elif period == "all":
        since = None
    else:
        raise ValueError(f"unknown period: {period}")
    return _aggregate(db, since, repo_full_name)


def list_repos_with_stats(db: Session) -> list[Repo]:
    """Repos that have at least one weekly stat — the only ones worth showing in the filter."""
    return list(
        db.execute(
            select(Repo)
            .join(WeeklyStat, WeeklyStat.repo_id == Repo.id)
            .group_by(Repo.id)
            .order_by(Repo.full_name)
        ).scalars()
    )


def last_sync(db: Session) -> SyncRun | None:
    return db.execute(
        select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()


def list_repos(db: Session) -> list[Repo]:
    return list(db.execute(select(Repo).order_by(Repo.full_name)).scalars())


def weekly_activity(db: Session, weeks_back: int = 26) -> list[WeeklyTotals]:
    """Org-wide weekly totals for the trend chart."""
    today = datetime.now(UTC).date()
    since = _week_start(today) - timedelta(weeks=weeks_back - 1)
    rows = db.execute(
        select(
            WeeklyStat.week_start,
            func.coalesce(func.sum(WeeklyStat.additions), 0).label("additions"),
            func.coalesce(func.sum(WeeklyStat.deletions), 0).label("deletions"),
            func.coalesce(func.sum(WeeklyStat.commits), 0).label("commits"),
            func.coalesce(func.sum(WeeklyStat.changed_files), 0).label("changed_files"),
        )
        .where(WeeklyStat.week_start >= since)
        .group_by(WeeklyStat.week_start)
        .order_by(WeeklyStat.week_start)
    ).all()
    return [
        WeeklyTotals(
            week_start=r.week_start,
            additions=r.additions,
            deletions=r.deletions,
            commits=r.commits,
            changed_files=r.changed_files,
        )
        for r in rows
    ]


def top_repos(db: Session, since: date | None, limit: int = 10) -> list[RepoActivity]:
    stmt = (
        select(
            Repo.full_name,
            func.coalesce(func.sum(WeeklyStat.additions), 0).label("additions"),
            func.coalesce(func.sum(WeeklyStat.deletions), 0).label("deletions"),
            func.coalesce(func.sum(WeeklyStat.commits), 0).label("commits"),
        )
        .join(WeeklyStat, WeeklyStat.repo_id == Repo.id)
        .group_by(Repo.id)
        .order_by(func.sum(WeeklyStat.commits).desc())
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(WeeklyStat.week_start >= since)
    rows = db.execute(stmt).all()
    return [
        RepoActivity(
            full_name=r.full_name,
            additions=r.additions,
            deletions=r.deletions,
            commits=r.commits,
        )
        for r in rows
    ]


def period_totals(db: Session, since: date, until: date | None = None) -> PeriodTotals:
    stmt = select(
        func.coalesce(func.sum(WeeklyStat.additions), 0),
        func.coalesce(func.sum(WeeklyStat.deletions), 0),
        func.coalesce(func.sum(WeeklyStat.commits), 0),
        func.coalesce(func.sum(WeeklyStat.changed_files), 0),
        func.count(func.distinct(WeeklyStat.contributor_id)),
    ).where(WeeklyStat.week_start >= since)
    if until is not None:
        stmt = stmt.where(WeeklyStat.week_start < until)
    row = db.execute(stmt).one()
    return PeriodTotals(
        additions=row[0],
        deletions=row[1],
        commits=row[2],
        changed_files=row[3],
        contributors=row[4],
    )


def list_repos_paginated(
    db: Session, page: int, per_page: int, q: str | None = None
) -> tuple[list[Repo], int]:
    base = select(Repo)
    count_stmt = select(func.count()).select_from(Repo)
    if q:
        pattern = f"%{q.strip().lower()}%"
        base = base.where(func.lower(Repo.full_name).like(pattern))
        count_stmt = count_stmt.where(func.lower(Repo.full_name).like(pattern))
    total = db.execute(count_stmt).scalar_one()
    rows = list(
        db.execute(
            base.order_by(Repo.full_name)
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).scalars()
    )
    return rows, total
