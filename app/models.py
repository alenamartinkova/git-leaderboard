from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Contributor(Base):
    __tablename__ = "contributors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    login: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    html_url: Mapped[str | None] = mapped_column(String(512))

    stats: Mapped[list["WeeklyStat"]] = relationship(back_populates="contributor")


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    fork: Mapped[bool] = mapped_column(Boolean, default=False)
    branch_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    stats: Mapped[list["WeeklyStat"]] = relationship(back_populates="repo")


class WeeklyStat(Base):
    """GitHub /stats/contributors returns weekly buckets per contributor per repo."""

    __tablename__ = "weekly_stats"
    __table_args__ = (
        UniqueConstraint("contributor_id", "repo_id", "week_start", name="uq_weekly_stat"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contributor_id: Mapped[int] = mapped_column(ForeignKey("contributors.id", ondelete="CASCADE"), nullable=False, index=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), nullable=False, index=True)
    week_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    additions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deletions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    commits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    changed_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    contributor: Mapped[Contributor] = relationship(back_populates="stats")
    repo: Mapped[Repo] = relationship(back_populates="stats")


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    repos_synced: Mapped[int] = mapped_column(Integer, default=0)
    total_repos: Mapped[int] = mapped_column(Integer, default=0)
    current_index: Mapped[int] = mapped_column(Integer, default=0)
    current_repo: Mapped[str | None] = mapped_column(String(512))
    scope: Mapped[str] = mapped_column(String(16), default="org")  # "org" or "repo"
    error: Mapped[str | None] = mapped_column(String(2048))
