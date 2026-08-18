import base64
import csv
import io
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from urllib.parse import urlencode

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session, init_db
from app.queries import (
    MONTHLY_METRICS,
    PEOPLE_SORTS,
    contributor_by_login,
    coverage,
    distinct_orgs,
    last_sync,
    leaderboard,
    list_repos_paginated,
    list_repos_with_stats,
    monthly_grid,
    months_back,
    people_monthly,
    people_overview,
    period_totals,
    person_repos,
    person_yearly,
    sort_people,
    top_repos,
    weekly_activity,
)
from app.scheduler import start_scheduler, stop_scheduler
from app.sync import is_syncing, run_sync, run_sync_repo, run_sync_repo_list

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(title="Git Leaderboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Cesty, ktoré musia ísť aj bez prihlásenia (healthcheck monitoringu / dockeru).
_AUTH_EXEMPT = {"/healthz"}


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """HTTP Basic login. Vypnutý, kým nie sú v .env vyplnené AUTH_USER aj AUTH_PASSWORD."""
    if (
        not settings.auth_enabled
        or request.method == "OPTIONS"          # CORS preflight nenesie prihlasovacie údaje
        or request.url.path in _AUTH_EXEMPT
    ):
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            user = password = ""
        # compare_digest na oboch poliach — konštantný čas, nech sa heslo nedá uhádnuť po znakoch
        ok_user = secrets.compare_digest(user.encode(), settings.auth_user.encode())
        ok_pass = secrets.compare_digest(password.encode(), settings.auth_password.encode())
        if ok_user and ok_pass:
            return await call_next(request)

    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Git Leaderboard"'},
    )


if settings.auth_enabled:
    logger.info("HTTP Basic auth enabled for user %r", settings.auth_user)
elif settings.auth_user or settings.auth_password:
    logger.warning("AUTH_USER aj AUTH_PASSWORD musia byť vyplnené — login zostáva vypnutý")
else:
    logger.warning("no AUTH_USER/AUTH_PASSWORD set — app is open to anyone who can reach it")

if settings.cors_origins:
    # Len na čítanie — /api/* je GET-only.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    logger.info("CORS enabled for %s", ", ".join(settings.cors_origins))


ORG_COOKIE = "org"


def known_orgs(db: Session) -> list[str]:
    """Orgy z configu + tie, čo sú v DB (aby staré dáta nezmizli po zmene configu)."""
    names = list(settings.org_names)
    lowered = {n.lower() for n in names}
    for name in distinct_orgs(db):
        if name.lower() not in lowered:
            names.append(name)
            lowered.add(name.lower())
    return names


def current_org(request: Request, db: Session = Depends(get_session)) -> str | None:
    """Vybraný org: ?org= má prednosť pred cookie. None = všetky dokopy.

    Vracia kanonický názov z configu/DB, takže na filtrovanie ide vždy tá istá
    podoba bez ohľadu na to, ako ho niekto napísal v URL.
    """
    raw = (request.query_params.get("org") or request.cookies.get(ORG_COOKIE) or "").strip()
    if not raw:
        return None
    for name in known_orgs(db):
        if name.lower() == raw.lower():
            return name
    return None


def _common_ctx(db: Session, org: str | None = None) -> dict:
    orgs = known_orgs(db)
    return {
        "org": org,
        "orgs": orgs,
        "org_label": org or (orgs[0] if len(orgs) == 1 else "všetky organizácie"),
        "last_sync": last_sync(db),
        "syncing": is_syncing(),
    }


@app.get("/select-org")
def select_org(
    request: Request,
    org: str = Query("", max_length=255),
    next_url: str = Query("/", alias="next", max_length=2048),
    db: Session = Depends(get_session),
):
    """Uloží výber organizácie do cookie a vráti používateľa tam, kde bol."""
    response = RedirectResponse(url=_safe_next(next_url), status_code=303)
    wanted = org.strip()
    match = next((n for n in known_orgs(db) if n.lower() == wanted.lower()), None) if wanted else None
    if match:
        response.set_cookie(
            ORG_COOKIE, match, max_age=365 * 24 * 3600, httponly=True, samesite="lax", path="/"
        )
    else:
        response.delete_cookie(ORG_COOKIE, path="/")
    return response


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    period: str = Query("week", pattern="^(week|month|all)$"),
    repo: str = Query("", max_length=512),
    db: Session = Depends(get_session),
    org: str | None = Depends(current_org),
):
    repo_filter = repo.strip() or None
    rows = leaderboard(db, period, repo_filter, org)
    return templates.TemplateResponse(
        request,
        "leaderboard.html",
        {
            **_common_ctx(db, org),
            "rows": rows,
            "period": period,
            "selected_repo": repo_filter or "",
            "repos": list_repos_with_stats(db, org),
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    weeks: int = Query(26, ge=4, le=104),
    db: Session = Depends(get_session),
    org: str | None = Depends(current_org),
):
    today = datetime.now(UTC).date()
    since_30 = today - timedelta(days=30)
    since_60 = today - timedelta(days=60)

    activity = weekly_activity(db, weeks, org)
    chart_data = {
        "labels": [w.week_start.isoformat() for w in activity],
        "additions": [w.additions for w in activity],
        "deletions": [w.deletions for w in activity],
        "commits": [w.commits for w in activity],
        "changed_files": [w.changed_files for w in activity],
    }

    top_users = leaderboard(db, "month", org=org)[:10]
    contributors_chart = {
        "labels": [u.login for u in top_users],
        "commits": [u.commits for u in top_users],
        "additions": [u.additions for u in top_users],
    }

    repos = top_repos(db, since_30, 10, org)
    repos_chart = {
        "labels": [r.full_name for r in repos],
        "commits": [r.commits for r in repos],
        "additions": [r.additions for r in repos],
    }

    curr = period_totals(db, since_30, today, org)
    prev = period_totals(db, since_60, since_30, org)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            **_common_ctx(db, org),
            "weeks": weeks,
            "chart_data": chart_data,
            "contributors_chart": contributors_chart,
            "repos_chart": repos_chart,
            "curr": curr,
            "prev": prev,
        },
    )


@app.get("/repos", response_class=HTMLResponse)
def repos_page(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=100),
    q: str = Query("", max_length=200),
    db: Session = Depends(get_session),
    org: str | None = Depends(current_org),
):
    q_clean = q.strip()
    repos, total = list_repos_paginated(db, page, per_page, q_clean or None, org)
    total_pages = max(1, (total + per_page - 1) // per_page)
    return templates.TemplateResponse(
        request,
        "repos.html",
        {
            **_common_ctx(db, org),
            "repos": repos,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "q": q_clean,
        },
    )


# Rozsahy pre mesačnú mriežku: 0 = celá história.
MONTH_RANGES: dict[int, str] = {
    12: "posledných 12 mesiacov",
    24: "posledných 24 mesiacov",
    36: "posledných 36 mesiacov",
    0: "celá história",
}


@app.get("/people", response_class=HTMLResponse)
def people_page(
    request: Request,
    view: str = Query("months", pattern="^(months|totals)$"),
    sort: str = Query("additions", max_length=40),
    metric: str = Query("additions", max_length=40),
    months: int = Query(24, ge=0),
    db: Session = Depends(get_session),
    org: str | None = Depends(current_org),
):
    """Per-person stats — po mesiacoch (default) alebo ako súhrn za celú históriu."""
    if metric not in MONTHLY_METRICS:
        metric = "additions"
    if months not in MONTH_RANGES:
        months = 24

    ctx = {
        **_common_ctx(db, org),
        "coverage": coverage(db, org),
        "view": view,
        "sort": sort if sort in PEOPLE_SORTS else "additions",
        "sorts": PEOPLE_SORTS,
        "metric": metric,
        "metrics": MONTHLY_METRICS,
        "months": months,
        "month_ranges": MONTH_RANGES,
    }

    if view == "totals":
        ctx["rows"] = sort_people(people_overview(db, org=org), sort)
    else:
        since = months_back(months) if months else None
        month_cols, grid = monthly_grid(people_monthly(db, since, org=org), metric)
        ctx["month_cols"] = month_cols
        ctx["grid"] = grid
        ctx["rows"] = grid  # pre prázdny stav

    return templates.TemplateResponse(request, "people.html", ctx)


@app.get("/people.csv")
def people_csv(
    by: str = Query("month", pattern="^(month|total)$"),
    sort: str = Query("additions", max_length=40),
    months: int = Query(0, ge=0),
    db: Session = Depends(get_session),
    org: str | None = Depends(current_org),
):
    """Same numbers as /people, for anyone who wants them in a spreadsheet.

    ``by=month`` (default) gives one row per person per month — long format, čiže
    priamo použiteľné do kontingenčnej tabuľky. ``by=total`` dá súhrn za celú históriu.
    """
    if by == "month":
        return _people_monthly_csv(db, months, org)

    rows = sort_people(people_overview(db, org=org), sort)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "login", "first_week", "last_week", "active_weeks", "span_weeks", "repos",
        "commits", "additions", "deletions", "net_lines", "changed_files",
        "lines_per_commit", "files_per_commit", "commits_per_week", "additions_per_week",
    ])
    for r in rows:
        w.writerow([
            r.login,
            r.first_week or "", r.last_week or "", r.active_weeks, r.span_weeks, r.repos,
            r.commits, r.additions, r.deletions, r.net, r.changed_files,
            f"{r.lines_per_commit:.2f}", f"{r.files_per_commit:.2f}",
            f"{r.commits_per_week:.2f}", f"{r.additions_per_week:.2f}",
        ])

    return _csv_response(buf.getvalue(), "people.csv")


def _csv_response(content: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _people_monthly_csv(db: Session, months: int, org: str | None = None) -> Response:
    since = months_back(months) if months else None
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "org", "login", "month", "active_weeks", "repos", "commits",
        "additions", "deletions", "net_lines", "changed_files",
        "lines_per_commit", "files_per_commit",
    ])
    for r in people_monthly(db, since, org=org):
        w.writerow([
            org or "", r.login, r.month.strftime("%Y-%m"), r.active_weeks, r.repos, r.commits,
            r.additions, r.deletions, r.net, r.changed_files,
            f"{r.lines_per_commit:.2f}", f"{r.files_per_commit:.2f}",
        ])
    return _csv_response(buf.getvalue(), "people-monthly.csv")


@app.get("/api/people/monthly")
def api_people_monthly(
    months: int = Query(0, ge=0),
    login: str = Query("", max_length=255),
    db: Session = Depends(get_session),
    org: str | None = Depends(current_org),
):
    """Per-user, per-month čísla ako JSON — na postavenie vlastného FE.

    ``months=0`` (default) vráti celú históriu, inak posledných N mesiacov.
    ``login`` obmedzí výstup na jedného človeka.
    """
    since = months_back(months) if months else None
    who = login.strip() or None

    monthly = people_monthly(db, since, login=who, org=org)
    by_login: dict[str, list] = {}
    for r in monthly:
        by_login.setdefault(r.login, []).append(r)

    cov = coverage(db, org)
    people = []
    for row in people_overview(db, login=who, org=org):
        rows = by_login.get(row.login, [])
        people.append({
            "login": row.login,
            "avatar_url": row.avatar_url,
            "html_url": row.html_url,
            "first_activity": row.first_week.isoformat() if row.first_week else None,
            "last_activity": row.last_week.isoformat() if row.last_week else None,
            # Súčty za mesiace, ktoré sú v tejto odpovedi — nie za celú históriu,
            # aby sedeli s poľom `months` aj pri filtri.
            "totals_in_range": {
                "months": len(rows),
                "active_weeks": sum(r.active_weeks for r in rows),
                "commits": sum(r.commits for r in rows),
                "additions": sum(r.additions for r in rows),
                "deletions": sum(r.deletions for r in rows),
                "net_lines": sum(r.net for r in rows),
                "changed_files": sum(r.changed_files for r in rows),
            },
            "months": [
                {
                    "month": r.month.strftime("%Y-%m"),
                    "commits": r.commits,
                    "additions": r.additions,
                    "deletions": r.deletions,
                    "net_lines": r.net,
                    "changed_files": r.changed_files,
                    "repos": r.repos,
                    "active_weeks": r.active_weeks,
                    "lines_per_commit": round(r.lines_per_commit, 2),
                }
                for r in rows
            ],
        })
    people.sort(key=lambda p: p["totals_in_range"]["additions"], reverse=True)

    return {
        "org": org,
        "orgs": known_orgs(db),
        "generated_at": datetime.now(UTC).isoformat(),
        "range": {
            "months": months or None,
            "since": since.isoformat() if since else None,
        },
        "coverage": {
            "first_week": cov.first_week.isoformat() if cov.first_week else None,
            "repos_total": cov.repos_total,
            "repos_backfilled": cov.repos_backfilled,
            "complete": cov.complete,
        },
        "months": sorted({r.month.strftime("%Y-%m") for r in monthly}),
        "people": people,
    }


@app.get("/people/{login}", response_class=HTMLResponse)
def person_page(
    request: Request,
    login: str,
    db: Session = Depends(get_session),
    org: str | None = Depends(current_org),
):
    person = contributor_by_login(db, login)
    if person is None:
        raise HTTPException(status_code=404, detail=f"Contributor '{login}' nemá žiadne dáta.")

    rows = people_overview(db, login=person.login, org=org)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Contributor '{login}' nemá žiadne dáta.")
    row = rows[0]

    monthly = people_monthly(db, login=person.login, org=org)
    running = 0
    cumulative: list[int] = []
    for m in monthly:
        running += m.additions - m.deletions
        cumulative.append(running)

    chart = {
        "labels": [m.month.strftime("%Y-%m") for m in monthly],
        "additions": [m.additions for m in monthly],
        "deletions": [m.deletions for m in monthly],
        "commits": [m.commits for m in monthly],
        "changed_files": [m.changed_files for m in monthly],
        "cumulative_net": cumulative,
    }

    return templates.TemplateResponse(
        request,
        "person.html",
        {
            **_common_ctx(db, org),
            "person": person,
            "row": row,
            "coverage": coverage(db, org),
            "chart": chart,
            "months": list(reversed(monthly)),  # najnovší mesiac hore
            "years": person_yearly(db, person.id, org),
            "repos": person_repos(db, person.id, org=org),
        },
    )


@app.get("/sync/status", response_class=HTMLResponse)
def sync_status(
    request: Request,
    db: Session = Depends(get_session),
    org: str | None = Depends(current_org),
):
    syncing = is_syncing()
    response = templates.TemplateResponse(
        request,
        "_sync_footer.html",
        {"last_sync": last_sync(db), "syncing": syncing, "org": org},
    )
    response.headers["X-Syncing"] = "1" if syncing else "0"
    response.headers["Cache-Control"] = "no-store"
    return response


def _safe_next(url: str) -> str:
    """Only ever redirect back into this app."""
    if url.startswith("/") and not url.startswith("//"):
        return url
    return "/"


@app.post("/sync")
async def trigger_sync(
    background: BackgroundTasks,
    full: str = Form(""),
    org: str = Form(""),
    next: str = Form("/"),
):
    """Manual trigger — kicks the org-wide GitHub sync in the background.

    ``full=1`` re-reads every repo from its first commit; otherwise repos that
    already have full history only get the recent window topped up.
    """
    background.add_task(_safe_run_sync, bool(full), org.strip() or None)
    return RedirectResponse(url=_safe_next(next), status_code=303)


@app.post("/sync/repos")
async def trigger_sync_repo_list(
    background: BackgroundTasks,
    org: str = Form(""),
    next: str = Form("/repos"),
):
    """Fetch just the org's repo list (no commit history) so /repos fills up fast."""
    background.add_task(_safe_run_sync_repo_list, org.strip() or None)
    return RedirectResponse(url=_safe_next(next), status_code=303)


@app.post("/sync/repo")
async def trigger_sync_repo(
    background: BackgroundTasks,
    full_name: str = Form(...),
    full: str = Form(""),
    page: int = Form(1),
    per_page: int = Form(10),
    q: str = Form(""),
):
    """Manual trigger — sync a single repo by 'owner/repo'."""
    full_name = full_name.strip()
    background.add_task(_safe_run_sync_repo, full_name, bool(full))
    params = {"page": page, "per_page": per_page}
    if q:
        params["q"] = q
    return RedirectResponse(url=f"/repos?{urlencode(params)}", status_code=303)


async def _safe_run_sync(full: bool = False, org: str | None = None):
    try:
        await run_sync(full=full, org=org)
    except Exception:
        logger.exception("manual sync failed")


async def _safe_run_sync_repo_list(org: str | None = None):
    try:
        await run_sync_repo_list(org=org)
    except Exception:
        logger.exception("repo list sync failed")


async def _safe_run_sync_repo(full_name: str, full: bool = False):
    try:
        await run_sync_repo(full_name, full=full)
    except Exception:
        logger.exception("manual repo sync failed for %s", full_name)


@app.get("/healthz")
def healthz():
    return {"ok": True}
