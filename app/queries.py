from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Contributor, SyncRun, WeeklyStat


@dataclass
class LeaderboardRow:
    login: str
    avatar_url: str | None
    html_url: str | None
    additions: int
    deletions: int
    commits: int


def _aggregate(db: Session, since: date | None) -> list[LeaderboardRow]:
    stmt = (
        select(
            Contributor.login,
            Contributor.avatar_url,
            Contributor.html_url,
            func.coalesce(func.sum(WeeklyStat.additions), 0).label("additions"),
            func.coalesce(func.sum(WeeklyStat.deletions), 0).label("deletions"),
            func.coalesce(func.sum(WeeklyStat.commits), 0).label("commits"),
        )
        .join(WeeklyStat, WeeklyStat.contributor_id == Contributor.id)
        .group_by(Contributor.id)
        .order_by(func.sum(WeeklyStat.additions).desc())
    )
    if since is not None:
        stmt = stmt.where(WeeklyStat.week_start >= since)
    rows = db.execute(stmt).all()
    return [
        LeaderboardRow(
            login=r.login,
            avatar_url=r.avatar_url,
            html_url=r.html_url,
            additions=r.additions,
            deletions=r.deletions,
            commits=r.commits,
        )
        for r in rows
    ]


def _week_start(d: date) -> date:
    # GitHub buckets weeks at Sunday 00:00 UTC.
    return d - timedelta(days=(d.weekday() + 1) % 7)


def leaderboard(db: Session, period: str) -> list[LeaderboardRow]:
    today = datetime.now(UTC).date()
    if period == "week":
        since = _week_start(today)
    elif period == "month":
        since = today - timedelta(days=30)
    elif period == "all":
        since = None
    else:
        raise ValueError(f"unknown period: {period}")
    return _aggregate(db, since)


def last_sync(db: Session) -> SyncRun | None:
    return db.execute(
        select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()
