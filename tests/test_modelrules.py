"""What each provider's models accept (sst.modelrules), by model id: no request is sent."""
import pytest

from sst import modelrules


@pytest.mark.parametrize("model, version, temperature", [
    ("claude-haiku-4-5", (4, 5), True), ("claude-haiku-4-5-20251001", (4, 5), True),
    ("claude-sonnet-4-5-20250929", (4, 5), True), ("claude-sonnet-4-6", (4, 6), True), ("claude-opus-4-6", (4, 6), True),
    ("claude-sonnet-4-20250514", (4, 0), True), ("claude-opus-4-1-20250805", (4, 1), True),
    ("claude-3-5-haiku-20241022", (3, 5), True), ("claude-3-7-sonnet-latest", (3, 7), True),
    ("claude-opus-4-7", (4, 7), False), ("claude-opus-4-8", (4, 8), False), ("claude-opus-5", (5, 0), False),
    ("claude-sonnet-5", (5, 0), False), ("claude-sonnet-5-5", (5, 5), False), ("claude-opus-5-5", (5, 5), False),
    ("claude-fable-5", (5, 0), False), ("claude-fable-5-1", (5, 1), False),
    ("my-claude", None, False),  # unknown: the default temperature, which every model takes
])
def test_claude_4_7_and_later_take_no_temperature(model, version, temperature):
    assert modelrules.claude_version(model) == version
    assert modelrules.claude_takes_temperature(model) is temperature
