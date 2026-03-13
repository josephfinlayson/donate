import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import socketio
import stripe
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from .chat import get_bot_response, load_prompt
from .database import async_session, engine, Base
from .models import ChatSession
from .stripe_service import create_donation_checkout

stripe.api_key = os.getenv("STRIPE")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

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

# Socket.IO server — uses the default /socket.io path
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=[FRONTEND_URL],
    logger=False,
    engineio_logger=False,
)

# The top-level ASGI app: Socket.IO wraps FastAPI
# Socket.IO intercepts /socket.io/* requests, everything else goes to FastAPI
app = socketio.ASGIApp(sio, fastapi_app)


# --- Socket.IO Events ---


@sio.event
async def connect(sid, environ):
    print(f"Client connected: {sid}", flush=True)


@sio.event
async def start_session(sid, data):
    """Initialize a new chat session."""
    print(f"start_session called for {sid}", flush=True)
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
    print(f"Session {session_id} started, greeting sent", flush=True)


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
            except Exception as e:
                print(f"Stripe checkout creation failed: {e}", flush=True)

    response_data = {"message": bot_response}
    if checkout_url:
        response_data["checkout_url"] = checkout_url

    await sio.emit("bot_message", response_data, room=sid)


@sio.event
async def disconnect(sid):
    """Handle client disconnect — mark session completed."""
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

    print(f"Session {session_id} completed (disconnect)", flush=True)


# --- Stripe Webhook ---


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
            # Dev mode: parse without signature verification
            event = stripe.Event.construct_from(
                json.loads(payload), stripe.api_key
            )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        print(f"Webhook error: {e}", flush=True)
        return {"error": str(e)}, 400

    if event["type"] == "checkout.session.completed":
        checkout_session = event["data"]["object"]
        chat_session_id = checkout_session.get("metadata", {}).get("chat_session_id")

        if chat_session_id:
            amount = checkout_session.get("amount_total", 0) / 100  # cents to dollars

            async with async_session() as db:
                result = await db.execute(
                    select(ChatSession).where(
                        ChatSession.id == uuid.UUID(chat_session_id)
                    )
                )
                session = result.scalar_one_or_none()
                if session:
                    session.completed_payment = True
                    session.donated = True
                    session.donation_amount_usd = amount
                    session.reward_resolved_at = datetime.now(timezone.utc)
                    await db.commit()
                    print(
                        f"Donation recorded: session={chat_session_id}, "
                        f"amount=${amount}",
                        flush=True,
                    )

    return {"status": "ok"}


# --- Health Check ---


@fastapi_app.get("/health")
async def health():
    return {"status": "ok"}
