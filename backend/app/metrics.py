"""Composite metric for evaluating chat session outcomes.

This is the reward signal for the optimization pipeline.
The metric weights donation outcomes heavily but also rewards
intermediate funnel progress and engagement signals.
"""


def composite_score(session: dict) -> float:
    """Compute composite metric score for a session record.

    Args:
        session: Dict with session fields (donated, donation_amount_usd,
                 clicked_payment_link, started_checkout, message_count,
                 asked_about_charity, payment_link_shown)

    Returns:
        Float score. Higher = better outcome for the optimizer.
    """
    score = 0.0

    # Primary: donation (dominates the metric)
    if session.get("donated"):
        score += 10.0
        # Bonus for larger donations, capped at 5.0
        amount = session.get("donation_amount_usd", 0)
        score += min(amount / 50, 5.0)

    # Stripe funnel progression
    if session.get("clicked_payment_link"):
        score += 2.0
    if session.get("started_checkout"):
        score += 3.0

    # Engagement proxies (weighted lower to prevent gaming)
    msg_count = session.get("message_count", 0)
    score += min(msg_count / 20, 1.0)

    if session.get("asked_about_charity"):
        score += 1.0

    # Payment link shown is a mild signal — bot at least got to the ask
    if session.get("payment_link_shown"):
        score += 0.5

    return score


def session_to_record(session) -> dict:
    """Convert a ChatSession ORM object to the session record format
    used by the optimizer and metrics."""
    return {
        "session_id": str(session.id),
        "messages": session.messages,
        "prompt_version": session.prompt_version,
        "funnel": {
            "payment_link_shown": session.payment_link_shown,
            "clicked_payment_link": session.clicked_payment_link,
            "started_checkout": session.started_checkout,
            "completed_payment": session.completed_payment,
        },
        "donated": session.donated,
        "donation_amount_usd": session.donation_amount_usd,
        "message_count": session.message_count,
        "asked_about_charity": session.asked_about_charity,
        "composite_score": session.composite_score,
        "status": session.status,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "completed_at": session.completed_at.isoformat() if session.completed_at else None,
        "reward_resolved_at": session.reward_resolved_at.isoformat() if session.reward_resolved_at else None,
    }
