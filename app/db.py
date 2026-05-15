from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables if they don't exist. Replace with Alembic when schema starts changing."""
    from datetime import UTC, datetime

    from sqlalchemy import update

    from app import models

    Base.metadata.create_all(bind=engine)

    # Tiny inline migration so existing DBs gain the progress columns without Alembic.
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS total_repos INTEGER DEFAULT 0"))
        conn.execute(text("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS current_index INTEGER DEFAULT 0"))
        conn.execute(text("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS current_repo VARCHAR(512)"))
        conn.execute(text("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS scope VARCHAR(16) DEFAULT 'org'"))
        conn.execute(text("ALTER TABLE weekly_stats ADD COLUMN IF NOT EXISTS changed_files INTEGER NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE weekly_stats DROP COLUMN IF EXISTS branches_created"))
        conn.execute(text("ALTER TABLE repos ADD COLUMN IF NOT EXISTS branch_count INTEGER NOT NULL DEFAULT 0"))

    # Any sync run still marked as in-progress at startup was killed by a restart —
    # mark it so the UI doesn't show "running…" forever.
    with SessionLocal() as db:
        db.execute(
            update(models.SyncRun)
            .where(models.SyncRun.finished_at.is_(None))
            .values(finished_at=datetime.now(UTC), error="interrupted (app restart)")
        )
        db.commit()
