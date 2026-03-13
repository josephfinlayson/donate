import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

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
