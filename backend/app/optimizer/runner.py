"""GEPA optimization runner.

Takes accumulated session data, converts it to DSPy examples,
runs GEPA to produce candidate prompts, and returns results.

Each conversation is split into multiple training examples — one per bot turn.
Each example includes forward-looking outcome feedback: how many turns until
payment, how many turns of engagement remained, and whether the user donated.
This gives GEPA rich signal about which conversational moves lead to good outcomes.
"""

import json
import os
from pathlib import Path

import dspy

from .dspy_module import PersuasionChatbot

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def session_to_dspy_examples(session: dict) -> list[dspy.Example]:
    """Convert a session record to multiple DSPy Examples — one per bot turn.

    Each example represents a decision point: given the conversation so far
    and the user's message, what should the bot say? The feedback tells GEPA
    what happened after this point in the conversation.
    """
    messages = session.get("messages", [])
    donated = session.get("donated", False)
    donation_amount = session.get("donation_amount_usd", 0)
    total_messages = len(messages)
    clicked_link = session.get("funnel", {}).get("clicked_payment_link", False)
    started_checkout = session.get("funnel", {}).get("started_checkout", False)
    asked_about_charity = session.get("asked_about_charity", False)

    examples = []
    history_parts = []

    # Walk through the conversation, creating an example at each bot response
    i = 0
    while i < len(messages):
        msg = messages[i]

        if msg["role"] == "user":
            user_msg = msg["content"]
            # Look for the next bot response
            if i + 1 < len(messages) and messages[i + 1]["role"] == "bot":
                bot_msg = messages[i + 1]["content"]
                turns_remaining = (total_messages - (i + 2)) // 2  # user-bot pairs left
                turn_number = len(history_parts) // 2 + 1

                total_turns = total_messages // 2

                # Build feedback and score for this turn
                feedback = _build_feedback(
                    turn_number=turn_number,
                    turns_remaining=turns_remaining,
                    total_turns=total_turns,
                    donated=donated,
                    donation_amount=donation_amount,
                    clicked_link=clicked_link,
                )
                score = _compute_turn_score(
                    total_turns=total_turns,
                    donated=donated,
                    donation_amount=donation_amount,
                    clicked_link=clicked_link,
                    started_checkout=started_checkout,
                    asked_about_charity=asked_about_charity,
                )

                example = dspy.Example(
                    conversation_history="\n".join(history_parts) if history_parts else "(conversation start)",
                    user_message=user_msg,
                    bot_response=bot_msg,
                ).with_inputs("conversation_history", "user_message")

                example.feedback_text = feedback
                example.score_value = score

                examples.append(example)

                history_parts.append(f"User: {user_msg}")
                history_parts.append(f"Bot: {bot_msg}")
                i += 2
                continue

        # Bot message without preceding user (e.g. greeting) — add to history
        if msg["role"] == "bot":
            history_parts.append(f"Bot: {msg['content']}")
        i += 1

    return examples


def _compute_turn_score(
    total_turns: int,
    donated: bool,
    donation_amount: float,
    clicked_link: bool,
    started_checkout: bool,
    asked_about_charity: bool,
) -> float:
    """Compute a score for this turn based on overall session outcome.

    Every turn in a conversation gets the same score — the whole conversation
    contributed to the outcome, not just individual turns.

    Rewards (additive):
    - Engagement: total_turns / 10 (capped at 1.0)
    - Asked about charity: +0.3 (curiosity signal)
    - Clicked link: +0.5 (active interest)
    - Started checkout: +0.3 (commitment signal)
    - Donated: +3.0 (primary goal, dominates all other signals)
    - Donation amount: +min(amount / 10, 5.0)

    Max possible ~10.1. Donation always dominates engagement.
    """
    score = 0.0

    # Engagement: longer conversations = better (cap at 10 turns)
    score += min(total_turns / 10, 1.0)

    # User asked about the charity
    if asked_about_charity:
        score += 0.3

    # User actively clicked the payment link
    if clicked_link:
        score += 0.5

    # User started the checkout flow
    if started_checkout:
        score += 0.3

    # User donated
    if donated:
        score += 3.0
        score += min(donation_amount / 10, 5.0)

    return score


def _build_feedback(
    turn_number: int,
    turns_remaining: int,
    total_turns: int,
    donated: bool,
    donation_amount: float,
    clicked_link: bool,
) -> str:
    """Build forward-looking feedback for a specific point in the conversation."""
    parts = []

    parts.append(f"This is turn {turn_number} of {total_turns} in the conversation.")

    if donated:
        if turns_remaining <= 1:
            parts.append(f"The user donated ${donation_amount:.0f} very shortly after this exchange. This response was highly effective.")
        elif turns_remaining <= 3:
            parts.append(f"The user donated ${donation_amount:.0f} within {turns_remaining} turns after this point. The conversation was moving in the right direction.")
        else:
            parts.append(f"The user eventually donated ${donation_amount:.0f}, but it took {turns_remaining} more turns.")
    else:
        parts.append("The user did NOT donate in this session.")

    if turns_remaining > 0:
        parts.append(f"The user continued engaging for {turns_remaining} more turn(s) after this point.")
    else:
        parts.append("The user left after this exchange.")

    if clicked_link:
        if not donated:
            parts.append("The user clicked the payment link but did not complete the donation — they were close.")
        else:
            parts.append("The user clicked the payment link and completed the donation.")

    return " ".join(parts)


def conversation_metric(gold, pred, trace=None, pred_name=None, pred_trace=None):
    """GEPA metric with rich textual feedback.

    Returns dspy.Prediction(score, feedback) so GEPA's reflection LM
    can understand what happened in the conversation and why.

    Score is a continuous value combining engagement depth, link clicks,
    and donations. Feedback provides the rich context for reflection.
    """
    feedback = getattr(gold, "feedback_text", "No feedback available.")
    score = getattr(gold, "score_value", 0.0)
    return dspy.Prediction(score=score, feedback=feedback)


def run_gepa_optimization(
    sessions: list[dict],
    current_prompt: dict,
) -> dict:
    """Run GEPA optimization on accumulated session data.

    Args:
        sessions: List of session records from the API
        current_prompt: Current prompt JSON (with version, instructions, etc.)

    Returns:
        Dict with optimization results.
    """
    # Configure DSPy
    lm = dspy.LM(
        model="anthropic/claude-sonnet-4-6",
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        max_tokens=2048,
    )
    reflection_lm = dspy.LM(
        model="anthropic/claude-opus-4-6",
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        max_tokens=4096,
        temperature=1.0,
    )
    dspy.configure(lm=lm)

    # Filter out low-signal sessions (need at least one user→bot exchange beyond greeting)
    sessions = [s for s in sessions if s.get("message_count", 0) >= 3]

    # Convert sessions to DSPy examples (multiple per session)
    examples = []
    for s in sessions:
        examples.extend(session_to_dspy_examples(s))

    if not examples:
        return {
            "optimized_instructions": current_prompt.get("evolvable_instructions", ""),
            "before_instructions": current_prompt.get("evolvable_instructions", ""),
            "metrics": {"sessions_count": len(sessions), "examples_count": 0},
            "reflections": "",
            "improved": False,
        }

    # Split into train/val (80/20)
    split_idx = max(1, int(len(examples) * 0.8))
    train_set = examples[:split_idx]
    val_set = examples[split_idx:] if split_idx < len(examples) else examples[-3:]

    # Create the DSPy module with current prompt as initial instructions
    program = PersuasionChatbot()
    program.persuade.predict.signature = program.persuade.predict.signature.with_instructions(
        current_prompt.get("evolvable_instructions", "")
    )

    # Run GEPA
    optimizer = dspy.GEPA(
        metric=conversation_metric,
        auto="light",
        num_threads=4,
        track_stats=True,
        reflection_minibatch_size=min(3, len(train_set)),
        reflection_lm=reflection_lm,
    )

    optimized = optimizer.compile(
        program,
        trainset=train_set,
        valset=val_set,
    )

    # Extract results
    new_instructions = optimized.persuade.predict.signature.instructions

    # Get GEPA stats if available
    stats = {}
    reflections = ""
    if hasattr(optimized, "detailed_results"):
        results = optimized.detailed_results
        scores = getattr(results, "val_aggregate_scores", [])
        stats = {
            "best_aggregate_score": max(scores) if scores else None,
            "num_candidates": len(scores) if scores else None,
            "total_metric_calls": getattr(results, "total_metric_calls", None),
        }
        reflections = getattr(results, "reflections", "")

    return {
        "optimized_instructions": new_instructions,
        "before_instructions": current_prompt.get("evolvable_instructions", ""),
        "metrics": {
            "sessions_count": len(sessions),
            "examples_count": len(examples),
            "train_size": len(train_set),
            "val_size": len(val_set),
            **stats,
        },
        "reflections": reflections,
        "improved": new_instructions != current_prompt.get(
            "evolvable_instructions", ""
        ),
    }
