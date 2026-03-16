import json
import os
from pathlib import Path

import anthropic

PROMPTS_DIR = Path(__file__).parent / "prompts"

client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def load_prompt() -> dict:
    with open(PROMPTS_DIR / "current.json") as f:
        return json.load(f)


def build_system_prompt(prompt_data: dict) -> str:
    return (
        prompt_data["immutable_constraints"]
        + "\n\n---\n\n"
        + prompt_data["evolvable_instructions"]
    )


async def get_bot_response(
    messages: list[dict], system_prompt: str | None = None
) -> str:
    """Get a response from Claude given conversation history."""
    full_text = ""
    async for _chunk in stream_bot_response(messages, system_prompt):
        full_text += _chunk
    return full_text


async def stream_bot_response(
    messages: list[dict], system_prompt: str | None = None
):
    """Stream a response from Claude, yielding text chunks."""
    if system_prompt is None:
        prompt_data = load_prompt()
        system_prompt = build_system_prompt(prompt_data)

    # Convert our message format to Anthropic format
    anthropic_messages = []
    for msg in messages:
        role = "assistant" if msg["role"] == "bot" else "user"
        anthropic_messages.append({"role": role, "content": msg["content"]})

    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system_prompt,
        messages=anthropic_messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text
