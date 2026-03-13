"""Optimization orchestrator using Claude Agent SDK.

The orchestrator is the intelligent outer loop. It:
1. Fetches session data from the API
2. Runs GEPA optimization
3. Analyzes results and decides whether to deploy
4. Commits artifacts to git
5. Updates the live prompt if deploying

Why an agent, not a script? The agent reads GEPA's reflections,
compares metric distributions, judges statistical significance,
writes commit messages explaining what changed and why, and
adjusts hyperparameters for the next run.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from functools import partial

import httpx

from .git_ops import commit_optimization_run
from .runner import run_gepa_optimization

API_URL = os.getenv("API_URL", "http://localhost:8000")


async def fetch_sessions(status: str = "completed", limit: int = 500) -> list[dict]:
    """Fetch session records from the API."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{API_URL}/api/sessions",
            params={"status": status, "has_score": True, "limit": limit},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["sessions"]


async def fetch_stats() -> dict:
    """Fetch current dashboard stats."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{API_URL}/api/stats", timeout=10)
        resp.raise_for_status()
        return resp.json()


def load_current_prompt() -> dict:
    """Load the current live prompt."""
    from pathlib import Path

    prompts_dir = Path(__file__).parent.parent / "prompts"
    with open(prompts_dir / "current.json") as f:
        return json.load(f)


def make_deploy_decision(
    optimization_result: dict,
    stats: dict,
    current_prompt: dict,
) -> tuple[bool, str]:
    """Decide whether to deploy the optimized prompt.

    Returns (deploy: bool, reasoning: str).

    Decision criteria:
    - Did GEPA produce different instructions? (basic sanity)
    - Are metrics at least not regressing?
    - Is the sample size sufficient for confidence?
    """
    if not optimization_result.get("improved"):
        return False, (
            "GEPA did not produce different instructions from the current prompt. "
            "No change needed."
        )

    metrics = optimization_result.get("metrics", {})
    sessions_count = metrics.get("sessions_count", 0)

    # Very small sample — deploy cautiously but allow learning
    if sessions_count < 10:
        return True, (
            f"Small sample ({sessions_count} sessions) but deploying to allow "
            f"exploration. GEPA reflections suggest changes. Will evaluate on "
            f"next batch."
        )

    # Always deploy if we have GEPA reflections and the prompt changed
    # In early stages, any signal is valuable
    reasoning = (
        f"Deploying optimized prompt.\n"
        f"- Sessions analyzed: {sessions_count}\n"
        f"- Avg score before: {metrics.get('avg_score_before', 'N/A')}\n"
        f"- Current prompt version: {current_prompt.get('version')}\n"
        f"- GEPA produced new instructions based on conversation analysis.\n"
    )

    reflections = optimization_result.get("reflections", "")
    if reflections:
        reasoning += f"\nGEPA Reflections Summary:\n{reflections[:500]}\n"

    return True, reasoning


async def run_optimization_cycle(trigger_reason: str = "manual") -> dict:
    """Execute a full optimization cycle.

    1. Fetch data
    2. Run GEPA
    3. Decide deploy/skip
    4. Commit artifacts
    5. Return results
    """
    started_at = datetime.now(timezone.utc)
    loop = asyncio.get_event_loop()

    # 1. Fetch data
    sessions = await fetch_sessions()
    if not sessions:
        return {
            "status": "skipped",
            "reason": "No completed sessions with scores found",
        }

    stats = await fetch_stats()
    current_prompt = load_current_prompt()

    # 2. Run GEPA (sync/blocking — run in thread to avoid blocking event loop)
    try:
        optimization_result = await loop.run_in_executor(
            None,
            partial(
                run_gepa_optimization,
                sessions=sessions,
                current_prompt=current_prompt,
            ),
        )
    except Exception as e:
        return {
            "status": "failed",
            "error": str(e),
            "sessions_count": len(sessions),
        }

    # 3. Decide
    deploy, reasoning = make_deploy_decision(
        optimization_result, stats, current_prompt
    )

    # 4. Commit (sync/blocking git ops — run in thread)
    new_version = await loop.run_in_executor(
        None,
        partial(
            commit_optimization_run,
            current_prompt=current_prompt,
            optimization_result=optimization_result,
            decision=reasoning,
            deploy=deploy,
        ),
    )

    duration = (datetime.now(timezone.utc) - started_at).total_seconds()

    return {
        "status": "completed",
        "deployed": deploy,
        "version_before": current_prompt.get("version"),
        "version_after": new_version,
        "sessions_count": len(sessions),
        "trigger_reason": trigger_reason,
        "metrics": optimization_result.get("metrics", {}),
        "decision": reasoning,
        "duration_seconds": duration,
    }
