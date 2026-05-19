import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from urllib.parse import urlencode

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session, init_db
from app.queries import (
    last_sync,
    leaderboard,
    list_repos_paginated,
    list_repos_with_stats,
    period_totals,
    top_repos,
    weekly_activity,
)
from app.scheduler import start_scheduler, stop_scheduler
from app.sync import is_syncing, run_sync, run_sync_repo

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


def _common_ctx(db: Session) -> dict:
    return {
        "org": settings.github_org or "(unset)",
        "last_sync": last_sync(db),
        "syncing": is_syncing(),
    }


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    period: str = Query("week", pattern="^(week|month|all)$"),
    repo: str = Query("", max_length=512),
    db: Session = Depends(get_session),
):
    repo_filter = repo.strip() or None
    rows = leaderboard(db, period, repo_filter)
    return templates.TemplateResponse(
        request,
        "leaderboard.html",
        {
            **_common_ctx(db),
            "rows": rows,
            "period": period,
            "selected_repo": repo_filter or "",
            "repos": list_repos_with_stats(db),
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    weeks: int = Query(26, ge=4, le=104),
    db: Session = Depends(get_session),
):
    today = datetime.now(UTC).date()
    since_30 = today - timedelta(days=30)
    since_60 = today - timedelta(days=60)

    activity = weekly_activity(db, weeks)
    chart_data = {
        "labels": [w.week_start.isoformat() for w in activity],
        "additions": [w.additions for w in activity],
        "deletions": [w.deletions for w in activity],
        "commits": [w.commits for w in activity],
        "changed_files": [w.changed_files for w in activity],
    }

    top_users = leaderboard(db, "month")[:10]
    contributors_chart = {
        "labels": [u.login for u in top_users],
        "commits": [u.commits for u in top_users],
        "additions": [u.additions for u in top_users],
    }

    repos = top_repos(db, since_30, 10)
    repos_chart = {
        "labels": [r.full_name for r in repos],
        "commits": [r.commits for r in repos],
        "additions": [r.additions for r in repos],
    }

    curr = period_totals(db, since_30, today)
    prev = period_totals(db, since_60, since_30)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            **_common_ctx(db),
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
):
    q_clean = q.strip()
    repos, total = list_repos_paginated(db, page, per_page, q_clean or None)
    total_pages = max(1, (total + per_page - 1) // per_page)
    return templates.TemplateResponse(
        request,
        "repos.html",
        {
            **_common_ctx(db),
            "repos": repos,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "q": q_clean,
        },
    )


@app.get("/sync/status", response_class=HTMLResponse)
def sync_status(request: Request, db: Session = Depends(get_session)):
    syncing = is_syncing()
    response = templates.TemplateResponse(
        request,
        "_sync_footer.html",
        {"last_sync": last_sync(db), "syncing": syncing},
    )
    response.headers["X-Syncing"] = "1" if syncing else "0"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/sync")
async def trigger_sync(background: BackgroundTasks):
    """Manual trigger — kicks the full GitHub sync in the background."""
    background.add_task(_safe_run_sync)
    return RedirectResponse(url="/", status_code=303)


@app.post("/sync/repo")
async def trigger_sync_repo(
    background: BackgroundTasks,
    full_name: str = Form(...),
    page: int = Form(1),
    per_page: int = Form(10),
    q: str = Form(""),
):
    """Manual trigger — sync a single repo by 'owner/repo'."""
    full_name = full_name.strip()
    background.add_task(_safe_run_sync_repo, full_name)
    params = {"page": page, "per_page": per_page}
    if q:
        params["q"] = q
    return RedirectResponse(url=f"/repos?{urlencode(params)}", status_code=303)


async def _safe_run_sync():
    try:
        await run_sync()
    except Exception:
        logger.exception("manual sync failed")


async def _safe_run_sync_repo(full_name: str):
    try:
        await run_sync_repo(full_name)
    except Exception:
        logger.exception("manual repo sync failed for %s", full_name)


@app.get("/healthz")
def healthz():
    return {"ok": True}
