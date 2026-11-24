"""TASK-4631 (epic 4562 W2.7, H-010): the README never states how many tools there are, nor a version.

A hand-typed count ("fifty-one tools") and a hand-typed version ("Version 0.2.5") go stale the day a
tool is added or a release is cut, and the public GitHub README is what a buyer reads. The README
links to the live list instead (tools/list, and its checked-in copy tests/snapshots/tools.json).
"""

import re
from pathlib import Path

import pytest

README = Path(__file__).resolve().parents[1] / "README.md"
# a number, in digits or words ("51 tools", "fifty-one tools", "twenty tools"), followed by "tools"
TOOL_COUNT = re.compile(r"\b([0-9]+|[a-z]+ty(-[a-z]+)?) tools\b", re.IGNORECASE)
# a stated server version ("Version 0.2.5", "version: 0.2", "v0.2.5 exposes")
VERSION = re.compile(r"\b(version\W{0,4}v?\d+\.\d+|v\d+\.\d+\.\d+)", re.IGNORECASE)


def _hits(pattern, text):
    return [line for line in text.splitlines() if pattern.search(line)]


def test_readme_states_no_tool_count():
    assert _hits(TOOL_COUNT, README.read_text()) == []


def test_readme_states_no_version():
    assert _hits(VERSION, README.read_text()) == []


def test_readme_links_the_live_tool_list():
    text = README.read_text()
    assert "tools/list" in text and "tests/snapshots/tools.json" in text


@pytest.mark.parametrize(
    "line",
    [
        "Version 0.2.5 exposes fifty-one tools, all listed below",
        "The server has 51 tools.",
        "all 28 tools, the access level each needs",
        "It offers twenty tools.",
        "Server version: 0.2.0",
    ],
)
def test_the_checks_catch_a_count_or_a_version(line):
    """Negative control: each shape that went stale before is caught."""
    assert _hits(TOOL_COUNT, line) or _hits(VERSION, line)


@pytest.mark.parametrize("line", ["Every tool is listed below.", "A tool call needs an agent key."])
def test_the_checks_pass_plain_wording(line):
    assert not _hits(TOOL_COUNT, line) and not _hits(VERSION, line)
