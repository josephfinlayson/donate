"""GEPA optimization runner.

Takes accumulated session data, converts it to DSPy examples,
runs GEPA to produce candidate prompts, and returns results.
"""

import json
import os
from pathlib import Path

import dspy

from .dspy_module import PersuasionChatbot

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def session_to_dspy_example(session: dict) -> dspy.Example:
    """Convert a session record to a DSPy Example for GEPA."""
    messages = session.get("messages", [])

    # Build conversation pairs for training
    # We use the full conversation as one example
    history_parts = []
    last_user_msg = ""
    last_bot_msg = ""

    for msg in messages:
        if msg["role"] == "user":
            last_user_msg = msg["content"]
            history_parts.append(f"User: {msg['content']}")
        elif msg["role"] == "bot":
            last_bot_msg = msg["content"]
            history_parts.append(f"Bot: {msg['content']}")

    # The example: given history up to second-to-last exchange,
    # predict the last bot response
    if len(messages) >= 4:
        # Use all but last exchange as history
        history = "\n".join(history_parts[:-2])
        user_msg = last_user_msg
        bot_msg = last_bot_msg
    else:
        # Short conversation: use initial greeting context
        history = ""
        user_msg = last_user_msg or "Hi"
        bot_msg = last_bot_msg or ""

    example = dspy.Example(
        conversation_history=history,
        user_message=user_msg,
        bot_response=bot_msg,
    ).with_inputs("conversation_history", "user_message")

    # Attach session metadata for the metric
    example.donated = session.get("donated", False)
    example.donation_amount_usd = session.get("donation_amount_usd", 0)
    example.clicked_payment_link = session.get("funnel", {}).get(
        "clicked_payment_link", False
    )
    example.started_checkout = session.get("funnel", {}).get(
        "started_checkout", False
    )
    example.message_count = session.get("message_count", 0)
    example.asked_about_charity = session.get("asked_about_charity", False)
    example.payment_link_shown = session.get("funnel", {}).get(
        "payment_link_shown", False
    )
    example.composite_score = session.get("composite_score", 0)

    return example


def composite_metric(gold, pred, trace=None, pred_name=None, pred_trace=None) -> float:
    """DSPy metric for GEPA (5-argument form required by GEPA).

    Uses pre-computed composite score from the session record.
    GEPA uses this to evaluate prompt candidates.
    """
    return getattr(gold, "composite_score", 0.0)


def run_gepa_optimization(
    sessions: list[dict],
    current_prompt: dict,
    max_metric_calls: int = 300,
) -> dict:
    """Run GEPA optimization on accumulated session data.

    Args:
        sessions: List of session records from the API
        current_prompt: Current prompt JSON (with version, instructions, etc.)
        max_metric_calls: Budget for GEPA metric evaluations

    Returns:
        Dict with optimization results:
        - optimized_prompt: The new prompt instructions (or None if no improvement)
        - metrics: Before/after scores
        - reflections: GEPA's natural-language analysis
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

    # Convert sessions to DSPy examples
    examples = [session_to_dspy_example(s) for s in sessions]

    # Split into train/val (80/20)
    split_idx = int(len(examples) * 0.8)
    train_set = examples[:split_idx] if split_idx > 0 else examples
    val_set = examples[split_idx:] if split_idx < len(examples) else examples[:5]

    # Create the DSPy module with current prompt as initial instructions
    program = PersuasionChatbot()
    # Set the current evolvable instructions as the signature instructions
    # In DSPy 3.x, ChainOfThought wraps a Predict module accessed via .predict
    program.persuade.predict.signature = program.persuade.predict.signature.with_instructions(
        current_prompt.get("evolvable_instructions", "")
    )

    # Run GEPA
    optimizer = dspy.GEPA(
        metric=composite_metric,
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
        stats = {
            "best_score": getattr(results, "best_score", None),
            "num_evaluations": getattr(results, "num_evaluations", None),
        }
        reflections = getattr(results, "reflections", "")

    # Compute before/after average scores
    before_scores = [composite_metric(ex, None, None, None, None) for ex in val_set]
    avg_before = sum(before_scores) / len(before_scores) if before_scores else 0

    return {
        "optimized_instructions": new_instructions,
        "before_instructions": current_prompt.get("evolvable_instructions", ""),
        "metrics": {
            "avg_score_before": avg_before,
            "sessions_count": len(sessions),
            "train_size": len(train_set),
            "val_size": len(val_set),
            **stats,
        },
        "reflections": reflections,
        "improved": new_instructions != current_prompt.get(
            "evolvable_instructions", ""
        ),
    }
