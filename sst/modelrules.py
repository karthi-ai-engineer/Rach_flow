"""What the providers' models accept in a request, so that each gets one that works and costs no more than it must.
From the providers' model pages, seen 2026-10-06 (the research notes "Rflow model costs and setups"); a model these rules
don't know keeps its provider's defaults, which always work.

AI cleanup, Text Transform and Translate ask through these (sst.gateway). Standard library only.
"""
import re

# ---- OpenAI (Chat Completions)

# Models that think before answering: o1, o3..., gpt-5 and later. They take only the default temperature (gpt-6's take
# one only at effort "none"), and their thinking counts against the token limit.
OPENAI_REASONING = re.compile(r"^(o\d|gpt-([5-9]|\d{2}))", re.IGNORECASE)
# The least thinking each allows, where it is known. gpt-6-luna, -sol, 6.1-sol and gpt-5.6-luna think at "medium" unless
# told, and allow "none"; gpt-6-astra goes no lower than "low", the first gpt-5 models no lower than "minimal". Others keep
# their default (gpt-5.4-mini and -nano think at "none" anyway; o3 and o4-mini aren't documented here).
_OPENAI_LEAST = [(re.compile(pattern, re.IGNORECASE), effort) for pattern, effort in [
    (r"^gpt-6(\.\d+)?-astra", "low"),
    (r"^gpt-6", "none"),
    (r"^gpt-5\.6-luna", "none"),
    (r"^gpt-5(-mini|-nano)?(-\d{4}-\d{2}-\d{2})?$", "minimal"),
]]


def openai_effort(model: str) -> str:
    """The `reasoning_effort` to ask an OpenAI model for: the least it allows, or "" to leave its default."""
    return next((effort for pattern, effort in _OPENAI_LEAST if pattern.search(model.strip())), "")


# ---- Anthropic

# claude-haiku-4-5, claude-sonnet-4-5-20250929, claude-opus-5-5, claude-fable-5-1, and the older claude-3-5-haiku-20241022
_CLAUDE = re.compile(r"claude-(?:[a-z]+-)?(\d+)(?:-(\d{1,2}))?(?:-[a-z]+)?(?:-\d{8}|-latest)?", re.IGNORECASE)


def claude_version(model: str) -> tuple[int, int] | None:
    """A Claude model's version from its id ((4, 5) for claude-haiku-4-5), or None for an id these rules can't read."""
    m = _CLAUDE.fullmatch(model.strip())
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def claude_takes_temperature(model: str) -> bool:
    """Whether a Claude model accepts temperature 0. Claude 4.7 and later (Sonnet 5, Opus 4.7 to 5.5, Fable) answer
    HTTP 400 to any sampling value but the default; some of them think whether asked or not (Opus 5.5)."""
    version = claude_version(model)
    return version is not None and version < (4, 7)
