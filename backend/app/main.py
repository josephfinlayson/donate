import asyncio
import json
import os
import random
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import socketio
import stripe
from fastapi import FastAPI, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, func, Integer

from .chat import get_bot_response, load_prompt, build_system_prompt
from .database import async_session, engine, Base
from .metrics import composite_score, session_to_record
from .models import ChatSession, FunnelEvent, OptimizationRun, ABTest
from .stripe_service import create_donation_checkout

stripe.api_key = os.getenv("STRIPE")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

# Optimization trigger thresholds
SCHEDULED_OPTIMIZATION_INTERVAL = int(
    os.getenv("OPTIMIZATION_INTERVAL_SECONDS", "3600")
)  # default: 1 hour
MIN_SESSIONS_FOR_OPTIMIZATION = int(os.getenv("MIN_SESSIONS_FOR_OPTIMIZATION", "20"))
ROLLBACK_CHECK_SESSIONS = int(os.getenv("ROLLBACK_CHECK_SESSIONS", "50"))
ROLLBACK_SCORE_THRESHOLD = float(os.getenv("ROLLBACK_SCORE_THRESHOLD", "0.7"))

# Track active sessions: sid -> session_id mapping
active_sessions: dict[str, str] = {}

# Prevent concurrent optimization runs
_optimization_lock = asyncio.Lock()


async def run_optimization_background(trigger_reason: str):
    """Run optimization in background, recording results to DB."""
    if _optimization_lock.locked():
        print("Optimization already in progress, skipping", flush=True)
        return

    async with _optimization_lock:
        from .optimizer.orchestrator import run_optimization_cycle

        print(f"Starting background optimization: {trigger_reason}", flush=True)
        try:
            result = await run_optimization_cycle(trigger_reason=trigger_reason)

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

            print(
                f"Background optimization done: {result.get('status')} "
                f"deployed={result.get('deployed', False)}",
                flush=True,
            )
        except Exception as e:
            print(f"Background optimization failed: {e}", flush=True)


async def check_rollback():
    """Check if the current prompt version is underperforming and rollback if needed.

    Compares the current version's recent scores against its parent version.
    If significantly worse after ROLLBACK_CHECK_SESSIONS, reverts to parent.
    """
    current = load_prompt()
    parent_version = current.get("parent_version")
    if not parent_version:
        return  # seed prompt, nothing to rollback to

    current_version = current.get("version")

    async with async_session() as db:
        # Get scores for current version
        current_result = await db.execute(
            select(ChatSession.composite_score)
            .where(
                ChatSession.prompt_version == current_version,
                ChatSession.status == "completed",
                ChatSession.composite_score.is_not(None),
            )
            .order_by(ChatSession.completed_at.desc())
            .limit(ROLLBACK_CHECK_SESSIONS)
        )
        current_scores = [r[0] for r in current_result]

        if len(current_scores) < ROLLBACK_CHECK_SESSIONS:
            return  # not enough data yet

        # Get scores for parent version
        parent_result = await db.execute(
            select(ChatSession.composite_score)
            .where(
                ChatSession.prompt_version == parent_version,
                ChatSession.status == "completed",
                ChatSession.composite_score.is_not(None),
            )
            .order_by(ChatSession.completed_at.desc())
            .limit(ROLLBACK_CHECK_SESSIONS)
        )
        parent_scores = [r[0] for r in parent_result]

        if not parent_scores:
            return

    avg_current = sum(current_scores) / len(current_scores)
    avg_parent = sum(parent_scores) / len(parent_scores)

    # Rollback if current is significantly worse
    if avg_parent > 0 and avg_current / avg_parent < ROLLBACK_SCORE_THRESHOLD:
        print(
            f"ROLLBACK: {current_version} (avg={avg_current:.3f}) "
            f"underperforms {parent_version} (avg={avg_parent:.3f}). "
            f"Reverting.",
            flush=True,
        )
        # Load parent prompt and set as current
        repo_dir = Path(os.getenv("PROMPT_REPO_DIR", "/app/prompt_repo"))
        parent_file = repo_dir / "prompts" / "history" / f"{parent_version}.json"
        if parent_file.exists():
            import shutil
            prompts_dir = Path(__file__).parent / "prompts"
            shutil.copy(parent_file, prompts_dir / "current.json")
            print(f"Rolled back to {parent_version}", flush=True)


async def scheduled_optimization_loop():
    """Background loop that periodically checks for optimization opportunities."""
    await asyncio.sleep(30)  # let the app fully start
    while True:
        try:
            # Check rollback first
            await check_rollback()

            # Check if enough sessions accumulated
            async with async_session() as db:
                last_run = await db.execute(
                    select(OptimizationRun)
                    .order_by(OptimizationRun.created_at.desc())
                    .limit(1)
                )
                last_opt = last_run.scalar_one_or_none()

                query = select(func.count(ChatSession.id)).where(
                    ChatSession.status == "completed",
                    ChatSession.composite_score.is_not(None),
                )
                if last_opt:
                    query = query.where(
                        ChatSession.completed_at > last_opt.created_at
                    )
                result = await db.execute(query)
                sessions_since = result.scalar()

            if sessions_since >= MIN_SESSIONS_FOR_OPTIMIZATION:
                await run_optimization_background("scheduled")

        except Exception as e:
            print(f"Scheduled optimization check error: {e}", flush=True)

        await asyncio.sleep(SCHEDULED_OPTIMIZATION_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Start the background optimization scheduler
    scheduler_task = asyncio.create_task(scheduled_optimization_loop())
    yield
    scheduler_task.cancel()


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


def load_prompt_version(version: str) -> dict | None:
    """Load a specific prompt version from the prompt repo or current."""
    prompt_data = load_prompt()
    if prompt_data.get("version") == version:
        return prompt_data

    # Check prompt repo history
    repo_dir = Path(os.getenv("PROMPT_REPO_DIR", "/app/prompt_repo"))
    history_file = repo_dir / "prompts" / "history" / f"{version}.json"
    if history_file.exists():
        with open(history_file) as f:
            return json.load(f)

    return None


async def get_ab_test_prompt() -> tuple[dict, str | None]:
    """Get the prompt to use, considering active A/B tests.

    Returns (prompt_data, ab_test_variant) where variant is 'A', 'B', or None.
    """
    async with async_session() as db:
        result = await db.execute(
            select(ABTest).where(ABTest.status == "active").limit(1)
        )
        ab_test = result.scalar_one_or_none()

    if not ab_test:
        return load_prompt(), None

    # Random assignment
    if random.random() < ab_test.traffic_split:
        variant = "B"
        prompt = load_prompt_version(ab_test.variant_b_version)
    else:
        variant = "A"
        prompt = load_prompt_version(ab_test.variant_a_version)

    return prompt or load_prompt(), variant


async def record_funnel_event(
    session_id: str, event_type: str, metadata: dict | None = None
):
    """Record a funnel event with timestamp."""
    async with async_session() as db:
        event = FunnelEvent(
            session_id=session_id,
            event_type=event_type,
            event_data=metadata,
        )
        db.add(event)
        await db.commit()


async def compute_and_store_score(session_id: str):
    """Compute composite metric and store on session."""
    async with async_session() as db:
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )
        session = result.scalar_one_or_none()
        if session:
            record = session_to_record(session)
            session.composite_score = composite_score(record)
            await db.commit()
            return session.composite_score
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

    prompt_data, ab_variant = await get_ab_test_prompt()
    system_prompt = build_system_prompt(prompt_data)

    async with async_session() as db:
        chat_session = ChatSession(
            id=session_id,
            prompt_version=prompt_data["version"],
            messages=[],
        )
        db.add(chat_session)
        await db.commit()

    # Generate opening message from the bot
    opening = await get_bot_response(
        [{"role": "user", "content": "Hi, I just arrived at the page."}],
        system_prompt=system_prompt,
    )

    # Store the opening exchange
    messages = [
        {"role": "user", "content": "Hi, I just arrived at the page."},
        {"role": "bot", "content": opening},
    ]
    async with async_session() as db:
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == session_id)
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

    user_message = data.get("message", "").strip()[:2000]
    if not user_message:
        return

    async with async_session() as db:
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )
        session = result.scalar_one()
        messages = list(session.messages)

        # Cap at 50 messages to prevent runaway API costs
        if len(messages) >= 50:
            await sio.emit(
                "bot_message",
                {"message": "Thanks for chatting! This session has reached its limit. Feel free to start a new conversation anytime."},
                room=sid,
            )
            return

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

        # Get bot response using the session's assigned prompt version
        session_prompt = load_prompt_version(session.prompt_version)
        system_prompt = build_system_prompt(session_prompt) if session_prompt else None
        bot_response = await get_bot_response(messages, system_prompt=system_prompt)
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
            select(ChatSession).where(ChatSession.id == session_id)
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
            select(ChatSession).where(ChatSession.id == session_id)
        )
        session = result.scalar_one_or_none()
        if session and session.status == "active":
            session.status = "completed"
            session.completed_at = datetime.now(timezone.utc)
            await db.commit()

    # Compute composite score
    score = await compute_and_store_score(session_id)
    print(f"Session {session_id} completed (score={score})", flush=True)


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
                    ChatSession.id == chat_session_id
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

        # Immediate optimization trigger on donation (high-signal event)
        asyncio.create_task(run_optimization_background("donation_event"))

    elif event_type == "checkout.session.expired":
        async with async_session() as db:
            result = await db.execute(
                select(ChatSession).where(
                    ChatSession.id == chat_session_id
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
            select(ChatSession).where(ChatSession.id == session_id)
        )
        session = result.scalar_one_or_none()
        if not session:
            return JSONResponse({"error": "Session not found"}, status_code=404)

        # Get funnel events
        events_result = await db.execute(
            select(FunnelEvent)
            .where(FunnelEvent.session_id == session_id)
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
            "optimization_threshold": MIN_SESSIONS_FOR_OPTIMIZATION,
        }


# --- Optimization API ---


@fastapi_app.post("/api/optimize")
async def trigger_optimization(request: Request):
    """Manually trigger an optimization run."""
    from .optimizer.orchestrator import run_optimization_cycle

    if _optimization_lock.locked():
        return JSONResponse(
            {"status": "busy", "reason": "Optimization already running"},
            status_code=409,
        )

    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    trigger_reason = body.get("reason", "manual")

    async with _optimization_lock:
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


# --- A/B Test Management ---


@fastapi_app.get("/api/ab-tests")
async def list_ab_tests():
    """List all A/B tests."""
    async with async_session() as db:
        result = await db.execute(
            select(ABTest).order_by(ABTest.created_at.desc())
        )
        tests = result.scalars().all()
        return {
            "tests": [
                {
                    "id": str(t.id),
                    "name": t.name,
                    "status": t.status,
                    "variant_a_version": t.variant_a_version,
                    "variant_b_version": t.variant_b_version,
                    "traffic_split": t.traffic_split,
                    "created_at": t.created_at.isoformat(),
                    "ended_at": t.ended_at.isoformat() if t.ended_at else None,
                }
                for t in tests
            ]
        }


@fastapi_app.post("/api/ab-tests")
async def create_ab_test(request: Request):
    """Create a new A/B test between two prompt versions."""
    body = await request.json()
    name = body.get("name", "Unnamed test")
    variant_a = body.get("variant_a_version")
    variant_b = body.get("variant_b_version")
    traffic_split = body.get("traffic_split", 0.5)

    if not variant_a or not variant_b:
        return JSONResponse(
            {"error": "variant_a_version and variant_b_version required"},
            status_code=400,
        )

    async with async_session() as db:
        # Deactivate any existing active tests
        existing = await db.execute(
            select(ABTest).where(ABTest.status == "active")
        )
        for t in existing.scalars():
            t.status = "paused"

        test = ABTest(
            name=name,
            variant_a_version=variant_a,
            variant_b_version=variant_b,
            traffic_split=traffic_split,
        )
        db.add(test)
        await db.commit()
        await db.refresh(test)

        return {
            "id": str(test.id),
            "name": test.name,
            "status": test.status,
            "variant_a_version": test.variant_a_version,
            "variant_b_version": test.variant_b_version,
            "traffic_split": test.traffic_split,
        }


@fastapi_app.post("/api/ab-tests/{test_id}/stop")
async def stop_ab_test(test_id: str):
    """Stop an active A/B test."""
    async with async_session() as db:
        result = await db.execute(
            select(ABTest).where(ABTest.id == test_id)
        )
        test = result.scalar_one_or_none()
        if not test:
            return JSONResponse({"error": "Test not found"}, status_code=404)

        test.status = "completed"
        test.ended_at = datetime.now(timezone.utc)
        await db.commit()
        return {"status": "completed"}


@fastapi_app.get("/api/ab-tests/{test_id}/results")
async def ab_test_results(test_id: str):
    """Get comparative results for an A/B test."""
    async with async_session() as db:
        result = await db.execute(
            select(ABTest).where(ABTest.id == test_id)
        )
        test = result.scalar_one_or_none()
        if not test:
            return JSONResponse({"error": "Test not found"}, status_code=404)

        # Get stats for each variant
        variants = {}
        for label, version in [
            ("A", test.variant_a_version),
            ("B", test.variant_b_version),
        ]:
            query = select(ChatSession).where(
                ChatSession.prompt_version == version,
                ChatSession.status == "completed",
                ChatSession.created_at >= test.created_at,
            )
            sessions_result = await db.execute(query)
            sessions = sessions_result.scalars().all()

            total = len(sessions)
            donated = sum(1 for s in sessions if s.donated)
            scores = [s.composite_score for s in sessions if s.composite_score is not None]
            total_amount = sum(s.donation_amount_usd for s in sessions if s.donated)
            link_shown = sum(1 for s in sessions if s.payment_link_shown)
            clicked = sum(1 for s in sessions if s.clicked_payment_link)

            variants[label] = {
                "version": version,
                "sessions": total,
                "donations": donated,
                "donation_rate": round(donated / total, 4) if total > 0 else 0,
                "avg_score": round(sum(scores) / len(scores), 3) if scores else None,
                "total_donated_usd": round(total_amount, 2),
                "link_shown": link_shown,
                "clicked": clicked,
                "click_rate": round(clicked / link_shown, 4) if link_shown > 0 else 0,
            }

        return {
            "test": {
                "id": str(test.id),
                "name": test.name,
                "status": test.status,
                "traffic_split": test.traffic_split,
                "created_at": test.created_at.isoformat(),
            },
            "variants": variants,
        }


# --- Optimization History & GEPA Reflections ---


@fastapi_app.get("/api/optimization-runs")
async def list_optimization_runs(
    limit: int = Query(20, le=100),
):
    """List optimization run history."""
    async with async_session() as db:
        result = await db.execute(
            select(OptimizationRun)
            .order_by(OptimizationRun.created_at.desc())
            .limit(limit)
        )
        runs = result.scalars().all()
        return {
            "runs": [
                {
                    "id": str(r.id),
                    "prompt_version_before": r.prompt_version_before,
                    "prompt_version_after": r.prompt_version_after,
                    "sessions_count": r.sessions_count,
                    "donations_count": r.donations_count,
                    "avg_composite_score": r.avg_composite_score,
                    "trigger_reason": r.trigger_reason,
                    "status": r.status,
                    "deployed": r.deployed,
                    "run_metadata": r.run_metadata,
                    "created_at": r.created_at.isoformat(),
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                }
                for r in runs
            ]
        }


@fastapi_app.get("/api/prompt-history")
async def prompt_history():
    """Get prompt version history from the git repo."""
    repo_dir = Path(os.getenv("PROMPT_REPO_DIR", "/app/prompt_repo"))
    history_dir = repo_dir / "prompts" / "history"

    versions = []
    if history_dir.exists():
        for f in sorted(history_dir.glob("v*.json"), reverse=True):
            with open(f) as fh:
                data = json.load(fh)
                versions.append({
                    "version": data.get("version"),
                    "parent_version": data.get("parent_version"),
                    "created_at": data.get("created_at"),
                    "created_by": data.get("created_by"),
                    "instructions_preview": data.get("evolvable_instructions", "")[:200],
                })

    # Always include current
    current = load_prompt()
    if not versions or versions[0].get("version") != current.get("version"):
        versions.insert(0, {
            "version": current.get("version"),
            "parent_version": current.get("parent_version"),
            "created_at": current.get("created_at"),
            "created_by": current.get("created_by", "manual_seed"),
            "instructions_preview": current.get("evolvable_instructions", "")[:200],
        })

    return {"versions": versions}


@fastapi_app.get("/api/gepa-reflections")
async def gepa_reflections(limit: int = Query(10, le=50)):
    """Get GEPA reflections from the git repo run history."""
    repo_dir = Path(os.getenv("PROMPT_REPO_DIR", "/app/prompt_repo"))
    runs_dir = repo_dir / "runs"

    reflections = []
    if runs_dir.exists():
        run_dirs = sorted(runs_dir.iterdir(), reverse=True)
        for run_dir in run_dirs[:limit]:
            reflection_file = run_dir / "gepa_reflections.md"
            decision_file = run_dir / "decision.md"
            metrics_file = run_dir / "metrics.json"

            entry = {"timestamp": run_dir.name}

            if reflection_file.exists():
                entry["reflections"] = reflection_file.read_text()
            if decision_file.exists():
                entry["decision"] = decision_file.read_text()
            if metrics_file.exists():
                with open(metrics_file) as f:
                    entry["metrics"] = json.load(f)

            reflections.append(entry)

    return {"reflections": reflections}


@fastapi_app.get("/api/funnel-stats")
async def funnel_stats(prompt_version: str | None = Query(None)):
    """Get funnel conversion stats, optionally filtered by prompt version."""
    async with async_session() as db:
        query = select(ChatSession).where(ChatSession.status == "completed")
        if prompt_version:
            query = query.where(ChatSession.prompt_version == prompt_version)

        result = await db.execute(query)
        sessions = result.scalars().all()

        total = len(sessions)
        if total == 0:
            return {"total": 0, "funnel": {}}

        link_shown = sum(1 for s in sessions if s.payment_link_shown)
        clicked = sum(1 for s in sessions if s.clicked_payment_link)
        started_checkout = sum(1 for s in sessions if s.started_checkout)
        completed = sum(1 for s in sessions if s.completed_payment)
        asked_charity = sum(1 for s in sessions if s.asked_about_charity)

        return {
            "total": total,
            "prompt_version": prompt_version,
            "funnel": {
                "sessions": total,
                "asked_about_charity": {"count": asked_charity, "rate": round(asked_charity / total, 4)},
                "payment_link_shown": {"count": link_shown, "rate": round(link_shown / total, 4)},
                "clicked_payment_link": {"count": clicked, "rate": round(clicked / total, 4)},
                "started_checkout": {"count": started_checkout, "rate": round(started_checkout / total, 4)},
                "completed_payment": {"count": completed, "rate": round(completed / total, 4)},
            },
        }


# --- Health Check ---


@fastapi_app.get("/health")
async def health():
    return {"status": "ok"}
