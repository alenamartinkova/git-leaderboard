from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Date, Integer, cast, func, select
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



# ---------------------------------------------------------------------------
# Per-person stats (celá história od prvého commitu)
# ---------------------------------------------------------------------------


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


@dataclass
class PersonRow:
    """All-time stats for one contributor."""

    login: str
    avatar_url: str | None
    html_url: str | None
    first_week: date | None
    last_week: date | None
    active_weeks: int
    repos: int
    commits: int
    additions: int
    deletions: int
    changed_files: int

    @property
    def net(self) -> int:
        return self.additions - self.deletions

    @property
    def lines_per_commit(self) -> float:
        return _ratio(self.additions, self.commits)

    @property
    def files_per_commit(self) -> float:
        return _ratio(self.changed_files, self.commits)

    @property
    def commits_per_week(self) -> float:
        return _ratio(self.commits, self.active_weeks)

    @property
    def additions_per_week(self) -> float:
        return _ratio(self.additions, self.active_weeks)

    @property
    def span_weeks(self) -> int:
        """Kalendárne týždne od prvej po poslednú aktivitu."""
        if not self.first_week or not self.last_week:
            return 0
        return (self.last_week - self.first_week).days // 7 + 1


@dataclass
class YearTotals:
    year: int
    active_weeks: int
    additions: int
    deletions: int
    commits: int
    changed_files: int

    @property
    def lines_per_commit(self) -> float:
        return _ratio(self.additions, self.commits)


@dataclass
class PersonRepo:
    full_name: str
    additions: int
    deletions: int
    commits: int
    changed_files: int
    first_week: date
    last_week: date


@dataclass
class Coverage:
    """Ako ďaleko dozadu dátam veriť."""

    first_week: date | None
    repos_total: int
    repos_backfilled: int

    @property
    def repos_pending(self) -> int:
        return self.repos_total - self.repos_backfilled

    @property
    def complete(self) -> bool:
        return self.repos_total > 0 and self.repos_pending == 0


def people_overview(
    db: Session,
    login: str | None = None,
    repo_full_name: str | None = None,
) -> list[PersonRow]:
    """Per-contributor totals over the whole stored history."""
    stmt = (
        select(
            Contributor.login,
            Contributor.avatar_url,
            Contributor.html_url,
            func.min(WeeklyStat.week_start).label("first_week"),
            func.max(WeeklyStat.week_start).label("last_week"),
            func.count(func.distinct(WeeklyStat.week_start)).label("active_weeks"),
            func.count(func.distinct(WeeklyStat.repo_id)).label("repos"),
            func.coalesce(func.sum(WeeklyStat.commits), 0).label("commits"),
            func.coalesce(func.sum(WeeklyStat.additions), 0).label("additions"),
            func.coalesce(func.sum(WeeklyStat.deletions), 0).label("deletions"),
            func.coalesce(func.sum(WeeklyStat.changed_files), 0).label("changed_files"),
        )
        .join(WeeklyStat, WeeklyStat.contributor_id == Contributor.id)
        .group_by(Contributor.id)
        .order_by(func.sum(WeeklyStat.additions).desc())
    )
    if login:
        stmt = stmt.where(func.lower(Contributor.login) == login.strip().lower())
    if repo_full_name:
        stmt = stmt.join(Repo, Repo.id == WeeklyStat.repo_id).where(Repo.full_name == repo_full_name)

    return [
        PersonRow(
            login=r.login,
            avatar_url=r.avatar_url,
            html_url=r.html_url,
            first_week=r.first_week,
            last_week=r.last_week,
            active_weeks=r.active_weeks,
            repos=r.repos,
            commits=r.commits,
            additions=r.additions,
            deletions=r.deletions,
            changed_files=r.changed_files,
        )
        for r in db.execute(stmt).all()
    ]


PEOPLE_SORTS: dict[str, str] = {
    "additions": "+ riadky",
    "deletions": "− riadky",
    "commits": "Commity",
    "changed_files": "Súbory",
    "active_weeks": "Aktívne týždne",
    "lines_per_commit": "Riadky / commit",
    "additions_per_week": "+ riadky / týždeň",
    "commits_per_week": "Commity / týždeň",
    "first_week": "Prvý commit",
    "login": "Meno",
}


def sort_people(rows: list[PersonRow], sort: str) -> list[PersonRow]:
    """Sort in Python — the list is one row per person, never big enough for SQL."""
    if sort not in PEOPLE_SORTS:
        sort = "additions"
    if sort == "login":
        return sorted(rows, key=lambda r: r.login.lower())
    if sort == "first_week":
        return sorted(rows, key=lambda r: (r.first_week is None, r.first_week or date.max))
    return sorted(rows, key=lambda r: getattr(r, sort), reverse=True)


def contributor_by_login(db: Session, login: str) -> Contributor | None:
    return db.execute(
        select(Contributor).where(func.lower(Contributor.login) == login.strip().lower())
    ).scalar_one_or_none()


def person_yearly(db: Session, contributor_id: int) -> list[YearTotals]:
    year = cast(func.extract("year", WeeklyStat.week_start), Integer).label("year")
    rows = db.execute(
        select(
            year,
            func.count(func.distinct(WeeklyStat.week_start)).label("active_weeks"),
            func.coalesce(func.sum(WeeklyStat.additions), 0).label("additions"),
            func.coalesce(func.sum(WeeklyStat.deletions), 0).label("deletions"),
            func.coalesce(func.sum(WeeklyStat.commits), 0).label("commits"),
            func.coalesce(func.sum(WeeklyStat.changed_files), 0).label("changed_files"),
        )
        .where(WeeklyStat.contributor_id == contributor_id)
        .group_by(year)
        .order_by(year)
    ).all()
    return [
        YearTotals(
            year=r.year,
            active_weeks=r.active_weeks,
            additions=r.additions,
            deletions=r.deletions,
            commits=r.commits,
            changed_files=r.changed_files,
        )
        for r in rows
    ]


def person_repos(db: Session, contributor_id: int, limit: int = 15) -> list[PersonRepo]:
    rows = db.execute(
        select(
            Repo.full_name,
            func.coalesce(func.sum(WeeklyStat.additions), 0).label("additions"),
            func.coalesce(func.sum(WeeklyStat.deletions), 0).label("deletions"),
            func.coalesce(func.sum(WeeklyStat.commits), 0).label("commits"),
            func.coalesce(func.sum(WeeklyStat.changed_files), 0).label("changed_files"),
            func.min(WeeklyStat.week_start).label("first_week"),
            func.max(WeeklyStat.week_start).label("last_week"),
        )
        .join(WeeklyStat, WeeklyStat.repo_id == Repo.id)
        .where(WeeklyStat.contributor_id == contributor_id)
        .group_by(Repo.id)
        .order_by(func.sum(WeeklyStat.commits).desc())
        .limit(limit)
    ).all()
    return [
        PersonRepo(
            full_name=r.full_name,
            additions=r.additions,
            deletions=r.deletions,
            commits=r.commits,
            changed_files=r.changed_files,
            first_week=r.first_week,
            last_week=r.last_week,
        )
        for r in rows
    ]


def coverage(db: Session) -> Coverage:
    first_week = db.execute(select(func.min(WeeklyStat.week_start))).scalar()
    repos_total = db.execute(select(func.count()).select_from(Repo)).scalar_one()
    repos_backfilled = db.execute(
        select(func.count()).select_from(Repo).where(Repo.history_synced_from.is_not(None))
    ).scalar_one()
    return Coverage(first_week=first_week, repos_total=repos_total, repos_backfilled=repos_backfilled)


@dataclass
class PersonMonth:
    """Jeden človek × jeden mesiac."""

    login: str
    month: date
    active_weeks: int
    repos: int
    commits: int
    additions: int
    deletions: int
    changed_files: int

    @property
    def net(self) -> int:
        return self.additions - self.deletions

    @property
    def lines_per_commit(self) -> float:
        return _ratio(self.additions, self.commits)

    @property
    def files_per_commit(self) -> float:
        return _ratio(self.changed_files, self.commits)


# Metriky, ktoré sa dajú zobraziť v mesačnej mriežke.
MONTHLY_METRICS: dict[str, str] = {
    "additions": "+ riadky",
    "deletions": "− riadky",
    "net": "Net riadky",
    "commits": "Commity",
    "changed_files": "Zmenené súbory",
    "lines_per_commit": "Riadky / commit",
}


def month_start(d: date) -> date:
    return d.replace(day=1)


def months_back(n: int, today: date | None = None) -> date:
    """Prvý deň mesiaca n-1 mesiacov dozadu (n=12 -> aktuálny + 11 predošlých)."""
    ref = month_start(today or datetime.now(UTC).date())
    total = ref.year * 12 + (ref.month - 1) - (n - 1)
    return date(total // 12, total % 12 + 1, 1)


def people_monthly(
    db: Session,
    since: date | None = None,
    login: str | None = None,
) -> list[PersonMonth]:
    """Per-contributor totals grouped by month.

    Týždeň patrí mesiacu, v ktorom začal — týždenný bucket sa nedá rozdeliť
    medzi dva mesiace, takže sa priraďuje celý podľa svojej nedele.
    """
    month = cast(func.date_trunc("month", WeeklyStat.week_start), Date).label("month")
    stmt = (
        select(
            Contributor.login,
            month,
            func.count(func.distinct(WeeklyStat.week_start)).label("active_weeks"),
            func.count(func.distinct(WeeklyStat.repo_id)).label("repos"),
            func.coalesce(func.sum(WeeklyStat.commits), 0).label("commits"),
            func.coalesce(func.sum(WeeklyStat.additions), 0).label("additions"),
            func.coalesce(func.sum(WeeklyStat.deletions), 0).label("deletions"),
            func.coalesce(func.sum(WeeklyStat.changed_files), 0).label("changed_files"),
        )
        .join(WeeklyStat, WeeklyStat.contributor_id == Contributor.id)
        .group_by(Contributor.id, month)
        .order_by(Contributor.login, month)
    )
    if since is not None:
        stmt = stmt.where(WeeklyStat.week_start >= since)
    if login:
        stmt = stmt.where(func.lower(Contributor.login) == login.strip().lower())

    return [
        PersonMonth(
            login=r.login,
            month=r.month,
            active_weeks=r.active_weeks,
            repos=r.repos,
            commits=r.commits,
            additions=r.additions,
            deletions=r.deletions,
            changed_files=r.changed_files,
        )
        for r in db.execute(stmt).all()
    ]


def monthly_grid(rows: list[PersonMonth], metric: str) -> tuple[list[date], list[dict]]:
    """Prepne dlhý zoznam (človek, mesiac) na mriežku ľudia × mesiace.

    Vracia (mesiace, riadky), kde každý riadok má bunky pre *všetky* mesiace —
    aj tie bez aktivity, aby stĺpce sedeli. ``intensity`` (0–1) je podiel voči
    najväčšej bunke v tabuľke, na jemné podfarbenie.
    """
    if metric not in MONTHLY_METRICS:
        metric = "additions"

    months = sorted({r.month for r in rows})
    by_login: dict[str, dict[date, PersonMonth]] = {}
    for r in rows:
        by_login.setdefault(r.login, {})[r.month] = r

    def value(pm: PersonMonth | None) -> float:
        if pm is None:
            return 0.0
        return float(abs(getattr(pm, metric)))

    peak = max((value(pm) for months_map in by_login.values() for pm in months_map.values()), default=0.0)

    grid: list[dict] = []
    for login, months_map in by_login.items():
        cells = []
        for m in months:
            pm = months_map.get(m)
            v = value(pm)
            cells.append({
                "month": m,
                "value": v,
                "stat": pm,
                "intensity": round(v / peak, 3) if peak else 0.0,
            })
        if metric == "lines_per_commit":
            # Pomer sa nesčítava — súhrn je pomer zo súčtov, nie súčet pomerov.
            adds = sum(pm.additions for pm in months_map.values())
            commits = sum(pm.commits for pm in months_map.values())
            total = _ratio(adds, commits)
        else:
            total = sum(c["value"] for c in cells)
        grid.append({"login": login, "cells": cells, "total": total})
    grid.sort(key=lambda r: r["total"], reverse=True)
    return months, grid
