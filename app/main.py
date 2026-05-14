import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session, init_db
from app.queries import last_sync, leaderboard
from app.scheduler import start_scheduler, stop_scheduler
from app.sync import run_sync

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


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    period: str = Query("week", pattern="^(week|month|all)$"),
    db: Session = Depends(get_session),
):
    rows = leaderboard(db, period)
    sync = last_sync(db)
    return templates.TemplateResponse(
        request,
        "leaderboard.html",
        {
            "rows": rows,
            "period": period,
            "org": settings.github_org or "(unset)",
            "last_sync": sync,
        },
    )


@app.post("/sync")
async def trigger_sync(background: BackgroundTasks):
    """Manual trigger — kicks the GitHub sync in the background."""
    background.add_task(_safe_run_sync)
    return RedirectResponse(url="/", status_code=303)


async def _safe_run_sync():
    try:
        await run_sync()
    except Exception:
        logger.exception("manual sync failed")


@app.get("/healthz")
def healthz():
    return {"ok": True}
