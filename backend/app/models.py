import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, JSON, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    prompt_version: Mapped[str] = mapped_column(String(50), default="v0.1.0")
    messages: Mapped[dict] = mapped_column(JSON, default=list)
    message_count: Mapped[int] = mapped_column(Integer, default=0)

    # Stripe
    stripe_checkout_session_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    stripe_checkout_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Funnel tracking
    payment_link_shown: Mapped[bool] = mapped_column(Boolean, default=False)
    clicked_payment_link: Mapped[bool] = mapped_column(Boolean, default=False)
    started_checkout: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_payment: Mapped[bool] = mapped_column(Boolean, default=False)

    # Outcome
    donated: Mapped[bool] = mapped_column(Boolean, default=False)
    donation_amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    asked_about_charity: Mapped[bool] = mapped_column(Boolean, default=False)

    # Composite metric score (computed on session completion)
    composite_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Lifecycle
    status: Mapped[str] = mapped_column(
        String(20), default="active"
    )  # active, completed, expired
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reward_resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    funnel_events: Mapped[list["FunnelEvent"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class FunnelEvent(Base):
    """Individual funnel events with timestamps for fine-grained tracking."""
    __tablename__ = "funnel_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id"), index=True
    )
    event_type: Mapped[str] = mapped_column(
        String(50)
    )  # payment_link_shown, clicked_link, started_checkout, completed_payment, checkout_expired
    event_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    session: Mapped["ChatSession"] = relationship(back_populates="funnel_events")


class OptimizationRun(Base):
    """Track optimization runs and their outcomes."""
    __tablename__ = "optimization_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    prompt_version_before: Mapped[str] = mapped_column(String(50))
    prompt_version_after: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    sessions_count: Mapped[int] = mapped_column(Integer)
    donations_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_composite_score: Mapped[float] = mapped_column(Float, default=0.0)
    trigger_reason: Mapped[str] = mapped_column(
        String(50)
    )  # batch_threshold, donation_event
    status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending, running, completed, failed
    deployed: Mapped[bool] = mapped_column(Boolean, default=False)
    run_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
