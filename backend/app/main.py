import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import socketio
import stripe
from fastapi import FastAPI, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, func, Integer

from .chat import get_bot_response, load_prompt
from .database import async_session, engine, Base
from .metrics import composite_score, session_to_record
from .models import ChatSession, FunnelEvent, OptimizationRun
from .stripe_service import create_donation_checkout

stripe.api_key = os.getenv("STRIPE")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

# Optimization trigger thresholds
BATCH_THRESHOLD = int(os.getenv("OPTIMIZATION_BATCH_THRESHOLD", "200"))

# Track active sessions: sid -> session_id mapping
active_sessions: dict[str, str] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


fastapi_app = FastAPI(title="Charity Persuasion Chatbot", lifespan=lifespan)

fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Socket.IO server
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=[FRONTEND_URL],
    logger=False,
    engineio_logger=False,
)
app = socketio.ASGIApp(sio, fastapi_app)


# --- Helpers ---


async def record_funnel_event(
    session_id: str, event_type: str, metadata: dict | None = None
):
    """Record a funnel event with timestamp."""
    async with async_session() as db:
        event = FunnelEvent(
            session_id=uuid.UUID(session_id),
            event_type=event_type,
            event_data=metadata,
        )
        db.add(event)
        await db.commit()


async def compute_and_store_score(session_id: str):
    """Compute composite metric and store on session."""
    async with async_session() as db:
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == uuid.UUID(session_id))
        )
        session = result.scalar_one_or_none()
        if session:
            record = session_to_record(session)
            session.composite_score = composite_score(record)
            await db.commit()
            return session.composite_score
    return None


async def check_optimization_trigger():
    """Check if we should trigger an optimization run.

    Triggers:
    1. Batch threshold: N completed sessions since last optimization
    2. Donation event: immediate trigger on donation (rare, high-signal)

    Returns trigger reason or None.
    """
    async with async_session() as db:
        # Get the last optimization run
        last_run = await db.execute(
            select(OptimizationRun)
            .order_by(OptimizationRun.created_at.desc())
            .limit(1)
        )
        last_run = last_run.scalar_one_or_none()

        # Count completed sessions since last run
        query = select(func.count(ChatSession.id)).where(
            ChatSession.status == "completed",
            ChatSession.composite_score.is_not(None),
        )
        if last_run:
            query = query.where(ChatSession.completed_at > last_run.created_at)

        result = await db.execute(query)
        sessions_since = result.scalar()

        if sessions_since >= BATCH_THRESHOLD:
            return "batch_threshold"

    return None


# --- Socket.IO Events ---


@sio.event
async def connect(sid, environ):
    print(f"Client connected: {sid}", flush=True)


@sio.event
async def start_session(sid, data):
    """Initialize a new chat session."""
    session_id = str(uuid.uuid4())
    active_sessions[sid] = session_id

    prompt_data = load_prompt()

    async with async_session() as db:
        chat_session = ChatSession(
            id=uuid.UUID(session_id),
            prompt_version=prompt_data["version"],
            messages=[],
        )
        db.add(chat_session)
        await db.commit()

    # Generate opening message from the bot
    opening = await get_bot_response(
        [{"role": "user", "content": "Hi, I just arrived at the page."}]
    )

    # Store the opening exchange
    messages = [
        {"role": "user", "content": "Hi, I just arrived at the page."},
        {"role": "bot", "content": opening},
    ]
    async with async_session() as db:
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == uuid.UUID(session_id))
        )
        session = result.scalar_one()
        session.messages = messages
        session.message_count = 1
        await db.commit()

    await sio.emit(
        "session_started",
        {"session_id": session_id, "message": opening},
        room=sid,
    )
    print(f"Session {session_id} started", flush=True)


@sio.event
async def send_message(sid, data):
    """Handle incoming user message."""
    session_id = active_sessions.get(sid)
    if not session_id:
        await sio.emit("error", {"message": "No active session"}, room=sid)
        return

    user_message = data.get("message", "").strip()
    if not user_message:
        return

    async with async_session() as db:
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == uuid.UUID(session_id))
        )
        session = result.scalar_one()
        messages = list(session.messages)

        # Add user message
        messages.append({"role": "user", "content": user_message})

        # Check if user is asking about the charity
        charity_keywords = [
            "givedirectly", "give directly", "charity", "how does it work",
            "where does the money go", "cash transfer", "evidence", "impact",
            "poverty", "effective",
        ]
        if any(kw in user_message.lower() for kw in charity_keywords):
            session.asked_about_charity = True

        # Get bot response
        bot_response = await get_bot_response(messages)
        messages.append({"role": "bot", "content": bot_response})

        session.messages = messages
        session.message_count = len([m for m in messages if m["role"] == "user"])
        await db.commit()

        # Check if bot mentioned donation / payment — if so, create checkout link
        checkout_url = None
        donation_keywords = [
            "donate", "donation", "contribute", "support",
            "give", "gift", "$",
        ]
        if (
            any(kw in bot_response.lower() for kw in donation_keywords)
            and not session.payment_link_shown
            and session.message_count >= 3
        ):
            try:
                checkout = await create_donation_checkout(session_id)
                session.stripe_checkout_session_id = checkout["checkout_session_id"]
                session.stripe_checkout_url = checkout["checkout_url"]
                session.payment_link_shown = True
                checkout_url = checkout["checkout_url"]
                await db.commit()
                await record_funnel_event(session_id, "payment_link_shown")
            except Exception as e:
                print(f"Stripe checkout creation failed: {e}", flush=True)

    response_data = {"message": bot_response}
    if checkout_url:
        response_data["checkout_url"] = checkout_url

    await sio.emit("bot_message", response_data, room=sid)


@sio.event
async def link_clicked(sid, data):
    """Track when user clicks the donation link."""
    session_id = active_sessions.get(sid)
    if not session_id:
        return

    async with async_session() as db:
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == uuid.UUID(session_id))
        )
        session = result.scalar_one_or_none()
        if session and not session.clicked_payment_link:
            session.clicked_payment_link = True
            await db.commit()
            await record_funnel_event(session_id, "clicked_link")
            print(f"Session {session_id}: user clicked payment link", flush=True)


@sio.event
async def disconnect(sid):
    """Handle client disconnect — mark session completed and compute score."""
    session_id = active_sessions.pop(sid, None)
    if not session_id:
        return

    async with async_session() as db:
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == uuid.UUID(session_id))
        )
        session = result.scalar_one_or_none()
        if session and session.status == "active":
            session.status = "completed"
            session.completed_at = datetime.now(timezone.utc)
            await db.commit()

    # Compute composite score
    score = await compute_and_store_score(session_id)
    print(f"Session {session_id} completed (score={score})", flush=True)

    # Check if we should trigger optimization
    trigger = await check_optimization_trigger()
    if trigger:
        print(f"OPTIMIZATION TRIGGER: {trigger}", flush=True)


# --- Stripe Webhooks ---


@fastapi_app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        else:
            event = stripe.Event.construct_from(
                json.loads(payload), stripe.api_key
            )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        print(f"Webhook error: {e}", flush=True)
        return JSONResponse({"error": str(e)}, status_code=400)

    event_type = event["type"]
    obj = event["data"]["object"]
    chat_session_id = obj.get("metadata", {}).get("chat_session_id")

    if not chat_session_id:
        return {"status": "ok"}

    if event_type == "checkout.session.completed":
        amount = obj.get("amount_total", 0) / 100

        async with async_session() as db:
            result = await db.execute(
                select(ChatSession).where(
                    ChatSession.id == uuid.UUID(chat_session_id)
                )
            )
            session = result.scalar_one_or_none()
            if session:
                session.started_checkout = True
                session.completed_payment = True
                session.donated = True
                session.donation_amount_usd = amount
                session.reward_resolved_at = datetime.now(timezone.utc)
                await db.commit()

        await record_funnel_event(
            chat_session_id,
            "completed_payment",
            {"amount_usd": amount},
        )

        # Recompute score with donation data
        score = await compute_and_store_score(chat_session_id)
        print(
            f"DONATION: session={chat_session_id}, amount=${amount}, score={score}",
            flush=True,
        )

        # Immediate optimization trigger on donation
        print("OPTIMIZATION TRIGGER: donation_event", flush=True)

    elif event_type == "checkout.session.expired":
        async with async_session() as db:
            result = await db.execute(
                select(ChatSession).where(
                    ChatSession.id == uuid.UUID(chat_session_id)
                )
            )
            session = result.scalar_one_or_none()
            if session:
                session.started_checkout = True
                session.reward_resolved_at = datetime.now(timezone.utc)
                await db.commit()

        await record_funnel_event(chat_session_id, "checkout_expired")
        await compute_and_store_score(chat_session_id)

    return {"status": "ok"}


# --- Data Export API (for optimizer) ---


@fastapi_app.get("/api/sessions")
async def list_sessions(
    status: str | None = Query(None),
    prompt_version: str | None = Query(None),
    has_score: bool = Query(False),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
):
    """Export session records for the optimization pipeline."""
    async with async_session() as db:
        query = select(ChatSession).order_by(ChatSession.created_at.desc())

        if status:
            query = query.where(ChatSession.status == status)
        if prompt_version:
            query = query.where(ChatSession.prompt_version == prompt_version)
        if has_score:
            query = query.where(ChatSession.composite_score.is_not(None))

        query = query.offset(offset).limit(limit)
        result = await db.execute(query)
        sessions = result.scalars().all()

        return {
            "sessions": [session_to_record(s) for s in sessions],
            "count": len(sessions),
        }


@fastapi_app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """Get a single session with full details including funnel events."""
    async with async_session() as db:
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == uuid.UUID(session_id))
        )
        session = result.scalar_one_or_none()
        if not session:
            return JSONResponse({"error": "Session not found"}, status_code=404)

        # Get funnel events
        events_result = await db.execute(
            select(FunnelEvent)
            .where(FunnelEvent.session_id == uuid.UUID(session_id))
            .order_by(FunnelEvent.created_at)
        )
        events = events_result.scalars().all()

        record = session_to_record(session)
        record["funnel_events"] = [
            {
                "event_type": e.event_type,
                "event_data": e.event_data,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ]
        return record


@fastapi_app.get("/api/stats")
async def get_stats():
    """Dashboard stats: session counts, donation rates, scores by prompt version."""
    async with async_session() as db:
        # Total sessions
        total = await db.execute(select(func.count(ChatSession.id)))
        total_count = total.scalar()

        # Completed sessions
        completed = await db.execute(
            select(func.count(ChatSession.id)).where(
                ChatSession.status == "completed"
            )
        )
        completed_count = completed.scalar()

        # Donations
        donations = await db.execute(
            select(func.count(ChatSession.id)).where(ChatSession.donated == True)
        )
        donation_count = donations.scalar()

        # Total donation amount
        total_donated = await db.execute(
            select(func.sum(ChatSession.donation_amount_usd)).where(
                ChatSession.donated == True
            )
        )
        total_donated_usd = total_donated.scalar() or 0.0

        # Average composite score
        avg_score = await db.execute(
            select(func.avg(ChatSession.composite_score)).where(
                ChatSession.composite_score.is_not(None)
            )
        )
        avg_composite = avg_score.scalar()

        # Per-prompt-version stats
        version_stats = await db.execute(
            select(
                ChatSession.prompt_version,
                func.count(ChatSession.id).label("count"),
                func.avg(ChatSession.composite_score).label("avg_score"),
                func.sum(
                    func.cast(ChatSession.donated, Integer)
                ).label("donations"),
            )
            .where(ChatSession.status == "completed")
            .group_by(ChatSession.prompt_version)
        )
        versions = [
            {
                "version": row.prompt_version,
                "sessions": row.count,
                "avg_score": round(row.avg_score, 3) if row.avg_score else None,
                "donations": row.donations or 0,
            }
            for row in version_stats
        ]

        # Sessions since last optimization
        last_run = await db.execute(
            select(OptimizationRun)
            .order_by(OptimizationRun.created_at.desc())
            .limit(1)
        )
        last_opt = last_run.scalar_one_or_none()

        sessions_since_opt_query = select(func.count(ChatSession.id)).where(
            ChatSession.status == "completed"
        )
        if last_opt:
            sessions_since_opt_query = sessions_since_opt_query.where(
                ChatSession.completed_at > last_opt.created_at
            )
        sessions_since = (await db.execute(sessions_since_opt_query)).scalar()

        return {
            "total_sessions": total_count,
            "completed_sessions": completed_count,
            "donations": donation_count,
            "donation_rate": (
                round(donation_count / completed_count, 4)
                if completed_count > 0
                else 0
            ),
            "total_donated_usd": round(total_donated_usd, 2),
            "avg_composite_score": (
                round(avg_composite, 3) if avg_composite else None
            ),
            "prompt_versions": versions,
            "sessions_since_last_optimization": sessions_since,
            "optimization_threshold": BATCH_THRESHOLD,
        }


# --- Optimization API ---


@fastapi_app.post("/api/optimize")
async def trigger_optimization(request: Request):
    """Manually trigger an optimization run."""
    from .optimizer.orchestrator import run_optimization_cycle

    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    trigger_reason = body.get("reason", "manual")

    result = await run_optimization_cycle(trigger_reason=trigger_reason)

    # Record the run in the database
    if result.get("status") == "completed":
        async with async_session() as db:
            run = OptimizationRun(
                prompt_version_before=result.get("version_before", ""),
                prompt_version_after=result.get("version_after"),
                sessions_count=result.get("sessions_count", 0),
                trigger_reason=trigger_reason,
                status="completed",
                deployed=result.get("deployed", False),
                run_metadata=result.get("metrics"),
            )
            db.add(run)
            await db.commit()

    return result


# --- Health Check ---


@fastapi_app.get("/health")
async def health():
    return {"status": "ok"}
