"""Git operations for prompt version control.

Every optimization run produces a commit, regardless of outcome.
Stores prompts, reflections, metrics, and decision reasoning.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

GIT_REPO_DIR = Path(os.getenv("PROMPT_REPO_DIR", "/app/prompt_repo"))
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
GITHUB_REPO = os.getenv("GITHUB_REPO", "git@github.com:josephfinlayson/donate.git")
SSH_KEY_PATH = os.getenv("SSH_KEY_PATH", "/app/.ssh/deploy_key")

# SSH command that uses the deploy key and skips host key checking
GIT_SSH_COMMAND = f"ssh -i {SSH_KEY_PATH} -o StrictHostKeyChecking=no"


def ensure_repo():
    """Ensure the prompt git repo exists and has the GitHub remote."""
    import subprocess

    GIT_REPO_DIR.mkdir(parents=True, exist_ok=True)
    (GIT_REPO_DIR / "prompts").mkdir(exist_ok=True)
    (GIT_REPO_DIR / "prompts" / "history").mkdir(exist_ok=True)
    (GIT_REPO_DIR / "runs").mkdir(exist_ok=True)

    if not (GIT_REPO_DIR / ".git").exists():
        subprocess.run(["git", "init"], cwd=GIT_REPO_DIR, check=True)
        subprocess.run(
            ["git", "config", "user.email", "optimizer@donate.bot"],
            cwd=GIT_REPO_DIR,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "GEPA Optimizer"],
            cwd=GIT_REPO_DIR,
            check=True,
        )

    # Ensure GitHub remote exists
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=GIT_REPO_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        subprocess.run(
            ["git", "remote", "add", "origin", GITHUB_REPO],
            cwd=GIT_REPO_DIR,
            check=True,
        )
    elif result.stdout.strip() != GITHUB_REPO:
        subprocess.run(
            ["git", "remote", "set-url", "origin", GITHUB_REPO],
            cwd=GIT_REPO_DIR,
            check=True,
        )


def bump_version(current_version: str) -> str:
    """Bump the patch version: v0.1.0 -> v0.1.1."""
    parts = current_version.lstrip("v").split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return "v" + ".".join(parts)


def commit_optimization_run(
    current_prompt: dict,
    optimization_result: dict,
    decision: str,
    deploy: bool,
) -> str:
    """Commit an optimization run to the git repo.

    Args:
        current_prompt: The prompt before optimization
        optimization_result: Results from GEPA
        decision: Agent's reasoning for deploy/skip
        deploy: Whether to deploy the new prompt

    Returns:
        The new version string (or current if not deployed)
    """
    import subprocess

    ensure_repo()

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M")
    run_dir = GIT_REPO_DIR / "runs" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    # Write run artifacts
    with open(run_dir / "config.json", "w") as f:
        json.dump(
            {
                "prompt_version_before": current_prompt.get("version"),
                "timestamp": timestamp,
                "metrics": optimization_result.get("metrics", {}),
            },
            f,
            indent=2,
        )

    with open(run_dir / "metrics.json", "w") as f:
        json.dump(optimization_result.get("metrics", {}), f, indent=2)

    reflections = optimization_result.get("reflections", "")
    with open(run_dir / "gepa_reflections.md", "w") as f:
        f.write(f"# GEPA Reflections — {timestamp}\n\n")
        f.write(reflections if reflections else "No reflections captured.\n")

    with open(run_dir / "candidate_prompt.json", "w") as f:
        json.dump(
            {
                "instructions": optimization_result.get("optimized_instructions"),
                "before_instructions": optimization_result.get("before_instructions"),
            },
            f,
            indent=2,
        )

    with open(run_dir / "decision.md", "w") as f:
        f.write(f"# Optimization Decision — {timestamp}\n\n")
        f.write(f"**Deploy**: {'YES' if deploy else 'NO'}\n\n")
        f.write(f"## Reasoning\n\n{decision}\n")

    new_version = current_prompt.get("version", "v0.1.0")

    if deploy:
        new_version = bump_version(new_version)

        # Create new prompt
        new_prompt = {
            "version": new_version,
            "immutable_constraints": current_prompt.get("immutable_constraints", ""),
            "evolvable_instructions": optimization_result.get(
                "optimized_instructions", ""
            ),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": "gepa_optimizer",
            "parent_version": current_prompt.get("version"),
        }

        # Save to prompt repo
        with open(
            GIT_REPO_DIR / "prompts" / "history" / f"{new_version}.json", "w"
        ) as f:
            json.dump(new_prompt, f, indent=2)

        with open(GIT_REPO_DIR / "prompts" / "current.json", "w") as f:
            json.dump(new_prompt, f, indent=2)

        # Also update the live prompt used by the backend
        with open(PROMPTS_DIR / "current.json", "w") as f:
            json.dump(new_prompt, f, indent=2)

    # Git commit
    subprocess.run(["git", "add", "-A"], cwd=GIT_REPO_DIR, check=True)

    commit_msg = (
        f"{'Deploy' if deploy else 'Skip'} {new_version}: "
        f"{optimization_result.get('metrics', {}).get('sessions_count', 0)} sessions"
        f"\n\n[skip ci]"
    )

    subprocess.run(
        ["git", "commit", "-m", commit_msg, "--allow-empty"],
        cwd=GIT_REPO_DIR,
        check=True,
    )

    # Push to GitHub
    try:
        env = {**os.environ, "GIT_SSH_COMMAND": GIT_SSH_COMMAND}
        # Push prompt_repo contents to a dedicated branch
        subprocess.run(
            ["git", "push", "-u", "origin", "HEAD:master"],
            cwd=GIT_REPO_DIR,
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
        print(f"Pushed optimization run to master", flush=True)
    except subprocess.CalledProcessError as e:
        print(f"Git push failed: {e.stderr}", flush=True)

    return new_version
