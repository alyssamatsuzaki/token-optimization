"""The local ledger (SPEC.md section 4).

SQLite, one file, single user. It holds every call Tokop has made on anyone's behalf, the
grades and scores computed over them, and the price snapshot each run was costed with.

Two design points matter beyond the obvious:

* **Raw usage is stored next to the normalized buckets on every call.** An auditor has to be
  able to redo the mapping from the provider's own payload, not take Tokop's word for it.
* **Prompts and outputs are separable from the metrics.** ``delete_run_content`` drops the
  stored request and response text for a run and leaves the numbers intact, so the deletion
  promise in SPEC.md non-negotiable 10 does not cost the user their results.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    inspect,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from tokop.core.usage import BUCKETS, TokenUsage
from tokop.paths import state_dir


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


class Workload(Base):
    __tablename__ = "workloads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    spec_path: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    runs: Mapped[list[Run]] = relationship(back_populates="workload")


class PriceSnapshotRow(Base):
    __tablename__ = "price_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    taken: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class Run(Base):
    """One pipeline on one split. The unit a proof compares."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workload_id: Mapped[str] = mapped_column(ForeignKey("workloads.id"))
    pipeline_id: Mapped[str] = mapped_column(String(64))
    split: Mapped[str] = mapped_column(String(32))
    model_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    price_snapshot_id: Mapped[str] = mapped_column(String(32), default="")
    seed: Mapped[int] = mapped_column(Integer, default=0)
    git_sha: Mapped[str] = mapped_column(String(64), default="")
    #: "live", "simulated" or "replay". Carried into every display of this run's numbers.
    origin: Mapped[str] = mapped_column(String(32), default="simulated")
    status: Mapped[str] = mapped_column(String(32), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_cost_usd: Mapped[str] = mapped_column(String(32), default="0")
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    workload: Mapped[Workload] = relationship(back_populates="runs")
    calls: Mapped[list[Call]] = relationship(back_populates="run", cascade="all, delete-orphan")
    grades: Mapped[list[GradeRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    scores: Mapped[list[ScoreRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    @property
    def cost(self) -> Decimal:
        return Decimal(self.total_cost_usd)


class Call(Base):
    """One provider call, with its raw usage and the cost computed from the run's snapshot."""

    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    #: Which cascade tier made this call; "single" for a non-cascade pipeline.
    tier: Mapped[str] = mapped_column(String(32), default="single")
    tier_index: Mapped[int] = mapped_column(Integer, default=0)
    #: Which step of the pipeline's graph made this call. A single-call pipeline compiles to a
    #: graph of one step, so every row a pre-M16 workload writes carries that step's id and
    #: nothing about it changes (UPGRADE_V4.md M16).
    step: Mapped[str] = mapped_column(String(64), default="generate")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_hash: Mapped[str] = mapped_column(String(64), default="")
    cassette_key: Mapped[str] = mapped_column(String(64), default="")
    reused: Mapped[bool] = mapped_column(Boolean, default=False)
    prewarm: Mapped[bool] = mapped_column(Boolean, default=False)

    input_uncached: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_5m: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_1h: Mapped[int] = mapped_column(Integer, default=0)
    cache_read: Mapped[int] = mapped_column(Integer, default=0)
    output_visible: Mapped[int] = mapped_column(Integer, default=0)
    output_reasoning: Mapped[int] = mapped_column(Integer, default=0)
    image_in: Mapped[int] = mapped_column(Integer, default=0)
    audio_in: Mapped[int] = mapped_column(Integer, default=0)
    image_out: Mapped[int] = mapped_column(Integer, default=0)

    usage_sources: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    raw_usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[str] = mapped_column(String(32), default="0")
    cost_by_bucket: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    price_snapshot_id: Mapped[str] = mapped_column(String(32), default="")
    provider_cost_usd: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cost_disagreement: Mapped[str | None] = mapped_column(String(32), nullable=True)

    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    ttft_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: "answered" or "escalated"; set by the cascade.
    decision: Mapped[str] = mapped_column(String(32), default="answered")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Deletable content (non-negotiable 10). Metrics above survive deletion.
    request_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    run: Mapped[Run] = relationship(back_populates="calls")

    @property
    def cost(self) -> Decimal:
        return Decimal(self.cost_usd)

    @property
    def usage(self) -> TokenUsage:
        return TokenUsage(
            **{b: getattr(self, b) for b in BUCKETS},
            sources=self.usage_sources,  # type: ignore[arg-type]
            raw=self.raw_usage,
        )

    def set_usage(self, usage: TokenUsage) -> None:
        for bucket in BUCKETS:
            setattr(self, bucket, getattr(usage, bucket))
        self.usage_sources = dict(usage.sources)
        self.raw_usage = dict(usage.raw)


class GradeRow(Base):
    """One task's outcome in one run."""

    __tablename__ = "grades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    correct: Mapped[bool] = mapped_column(Boolean)
    parsed: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    gold: Mapped[str] = mapped_column(Text, default="")
    answer_type: Mapped[str] = mapped_column(String(32), default="")
    question_type: Mapped[str] = mapped_column(String(32), default="")
    #: Which tier actually produced the returned answer.
    resolved_tier: Mapped[str] = mapped_column(String(32), default="single")
    task_cost_usd: Mapped[str] = mapped_column(String(32), default="0")

    run: Mapped[Run] = relationship(back_populates="grades")

    @property
    def cost(self) -> Decimal:
        return Decimal(self.task_cost_usd)


class ScoreRow(Base):
    """A scorer's or rater's estimate for one task at one tier."""

    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    tier: Mapped[str] = mapped_column(String(32), default="single")
    score: Mapped[float] = mapped_column(Float)
    features: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    #: "logistic" for the fitted scorer, "judge" for a model judge, "human" for a person.
    rater: Mapped[str] = mapped_column(String(32), default="logistic")
    sampling_probability: Mapped[float | None] = mapped_column(Float, nullable=True)

    run: Mapped[Run] = relationship(back_populates="scores")


class CompareSession(Base):
    __tablename__ = "compare_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    prompt: Mapped[str] = mapped_column(Text)
    models: Mapped[list[str]] = mapped_column(JSON, default=list)
    results: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    synthesis: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    origin: Mapped[str] = mapped_column(String(32), default="simulated")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Preference(Base):
    """ "Prefer this answer" on the Compare screen. Nothing reads it in v1 (SPEC.md 5.3)."""

    __tablename__ = "preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    compare_session_id: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Brief(Base):
    __tablename__ = "briefs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    brief_text: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(128))
    tokens_before: Mapped[int] = mapped_column(Integer, default=0)
    tokens_after: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[str] = mapped_column(String(32), default="0")
    origin: Mapped[str] = mapped_column(String(32), default="simulated")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


DB_FILENAME = "tokop.db"


def db_path() -> Path:
    return state_dir() / DB_FILENAME


class LedgerSchemaError(RuntimeError):
    """A ledger on disk predates a column the models declare, and cannot be read."""


def _check_schema(engine: Any) -> None:
    """Refuse a ledger whose tables are older than the models, and say how to fix it.

    ``create_all`` creates tables that are missing and leaves existing ones exactly as they are:
    it will not add a column. So a ledger generated before a column was added keeps working until
    something selects that column, and then fails with a bare SQL error naming neither the cause
    nor the remedy. The ledger is generated rather than committed — `tokop build-test-fixtures
    --ledger-only` rebuilds it from the cassettes in seconds — so the right behaviour is to say
    that, at the point the mismatch is first visible (M16b added ``calls.step``).
    """
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        on_disk = {column["name"] for column in inspector.get_columns(table.name)}
        missing = sorted(c.name for c in table.columns if c.name not in on_disk)
        if missing:
            raise LedgerSchemaError(
                f"the ledger's {table.name!r} table is missing {missing}, so it was generated "
                "before those columns existed. A ledger is generated, not committed: rebuild it "
                "with `tokop build-test-fixtures --ledger-only`, which replays the committed "
                "cassettes and spends nothing."
            )


def make_engine(path: Path | None = None, echo: bool = False) -> Any:
    target = path or db_path()
    engine = create_engine(f"sqlite:///{target}", echo=echo, future=True)
    Base.metadata.create_all(engine)
    _check_schema(engine)
    return engine


@contextmanager
def session_scope(engine: Any) -> Iterator[Session]:
    session = Session(engine, future=True)
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def delete_run_content(session: Session, run_id: str) -> int:
    """Drop stored prompts and outputs for one run, keeping every metric.

    SPEC.md non-negotiable 10. Costs, token counts, grades and scores are unaffected, so a user
    who deletes content does not lose the result they paid for.
    """
    calls = session.scalars(select(Call).where(Call.run_id == run_id)).all()
    for call in calls:
        call.request_json = None
        call.response_text = None
    run = session.get(Run, run_id)
    if run is not None:
        run.content_deleted = True
    return len(calls)


def delete_run(session: Session, run_id: str) -> bool:
    """Delete a run outright, with its calls, grades and scores."""
    run = session.get(Run, run_id)
    if run is None:
        return False
    session.execute(delete(Call).where(Call.run_id == run_id))
    session.execute(delete(GradeRow).where(GradeRow.run_id == run_id))
    session.execute(delete(ScoreRow).where(ScoreRow.run_id == run_id))
    session.delete(run)
    return True


def reset(engine: Any) -> None:
    """Drop and recreate every table. Used when rebuilding the ledger from fixtures."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def store_price_snapshot(
    session: Session, snapshot_id: str, taken: str, payload: dict[str, Any]
) -> None:
    existing = session.get(PriceSnapshotRow, snapshot_id)
    if existing is None:
        session.add(PriceSnapshotRow(id=snapshot_id, taken=taken, payload=payload))


def json_default(value: Any) -> str:
    """JSON encoder hook for Decimal and datetime, used when writing manifests."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=json_default)
