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
    payment_link_shown = session.get("funnel", {}).get("payment_link_shown", False)
    clicked_link = session.get("funnel", {}).get("clicked_payment_link", False)

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

                # Build feedback describing what happened after this point
                feedback = _build_feedback(
                    turn_number=turn_number,
                    turns_remaining=turns_remaining,
                    total_turns=total_messages // 2,
                    donated=donated,
                    donation_amount=donation_amount,
                    payment_link_shown=payment_link_shown,
                    clicked_link=clicked_link,
                )

                example = dspy.Example(
                    conversation_history="\n".join(history_parts) if history_parts else "(conversation start)",
                    user_message=user_msg,
                    bot_response=bot_msg,
                ).with_inputs("conversation_history", "user_message")

                example.feedback_text = feedback
                example.donated = donated
                example.score_value = 1.0 if donated else 0.0

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


def _build_feedback(
    turn_number: int,
    turns_remaining: int,
    total_turns: int,
    donated: bool,
    donation_amount: float,
    payment_link_shown: bool,
    clicked_link: bool,
) -> str:
    """Build forward-looking feedback for a specific point in the conversation."""
    parts = []

    parts.append(f"This is turn {turn_number} of {total_turns} in the conversation.")

    if donated:
        turns_until_payment = turns_remaining  # approximate
        if turns_until_payment <= 1:
            parts.append(f"The user donated ${donation_amount:.0f} very shortly after this exchange. This response was highly effective.")
        elif turns_until_payment <= 3:
            parts.append(f"The user donated ${donation_amount:.0f} within {turns_until_payment} turns after this point. The conversation was moving in the right direction.")
        else:
            parts.append(f"The user eventually donated ${donation_amount:.0f}, but it took {turns_until_payment} more turns. Earlier and more direct persuasion may have helped.")
    else:
        parts.append("The user did NOT donate in this session.")

    if turns_remaining > 0:
        parts.append(f"The user continued engaging for {turns_remaining} more turn(s) after this point.")
    else:
        parts.append("The user left after this exchange.")

    if payment_link_shown:
        if clicked_link:
            if not donated:
                parts.append("The user clicked the payment link but did not complete the donation — they were close but something stopped them.")
            else:
                parts.append("The user clicked the payment link and completed the donation.")
        else:
            parts.append("A payment link was shown but the user did not click it.")
    else:
        if total_turns <= 2:
            parts.append("The conversation was too short for a payment link to be shown.")
        else:
            parts.append("No payment link was shown during this session.")

    return " ".join(parts)


def conversation_metric(gold, pred, trace=None, pred_name=None, pred_trace=None):
    """GEPA metric with rich textual feedback.

    Returns dspy.Prediction(score, feedback) so GEPA's reflection LM
    can understand what happened in the conversation and why.
    The score is simple (donated=1, didn't=0). The feedback is where
    the real signal lives.
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
