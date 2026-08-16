"""Task -> model routing.

Defaults mirror the prior work's measured choices: volume work on a cheap
model (recent_news_agent came in at $0.03/lead), quality-visible work on a
strong one (a 51-lead email_writer run stayed under a dollar because only
the strategy/copy/critique steps used it).

Callers name a Task, never a model. The user overrides per task in config,
and an override always wins — including over a default that happens to be
newer, because the person paying for the tokens decides.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.llm.types import Task

# OpenRouter-style ids, since OpenRouter is the BYO default (one key, many
# models). Adapters for the direct providers map these to native ids.
DEFAULT_MODELS: Mapping[Task, str] = {
    Task.EXTRACT: "deepseek/deepseek-v4-flash",
    Task.CLASSIFY: "deepseek/deepseek-v4-flash",
    Task.RESEARCH: "anthropic/claude-haiku-4.5",
    Task.STRATEGY: "anthropic/claude-sonnet-5",
    Task.COPY: "anthropic/claude-sonnet-5",
    Task.CRITIQUE: "anthropic/claude-sonnet-5",
}


def model_for(task: Task, overrides: Mapping[str, str] | None = None) -> str:
    """The model id for a task. A user override beats the default."""
    if overrides:
        override = overrides.get(task.value) or overrides.get(task.name.lower())
        if override:
            return override
    return DEFAULT_MODELS[task]
