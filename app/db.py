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

    # Any sync run still marked as in-progress at startup was killed by a restart —
    # mark it so the UI doesn't show "running…" forever.
    with SessionLocal() as db:
        db.execute(
            update(models.SyncRun)
            .where(models.SyncRun.finished_at.is_(None))
            .values(finished_at=datetime.now(UTC), error="interrupted (app restart)")
        )
        db.commit()
