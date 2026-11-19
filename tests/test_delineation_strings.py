"""TASK-4535 (epic 4445 W7.5.5): every delineation, drainage, proposal and promotion sentence an agent relays is the
short text of the W7.5.0 table. ``data/w7_5_mcp_strings.json`` is the checked-in copy of the table's MCP rows.

The sentences are composed in code (f-strings), so each row is checked two ways: no run of words of its old text is left in
the source, and a clause of its new text is in it.
"""

import ast
import json
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "hydrata_mcp"
ROWS = json.loads((Path(__file__).parent / "data" / "w7_5_mcp_strings.json").read_text())["rows"]
KEPT = "(unchanged)"
CHANGED = [r for r in ROWS if r["new"] != KEPT]


def _source_text() -> str:
    """Every string the delineation tools hold, as Python reads them (adjacent literals and the fixed parts of an
    f-string joined in order), white space folded. Docstrings are tool descriptions, checked by the tool snapshot."""
    out = []
    for name in ("delineation.py", "terrain.py"):
        tree = ast.parse((SRC / name).read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                out.append(" ".join(node.value.split()))
    return " | ".join(out)


def _runs(text: str) -> list[str]:
    """The runs of fixed words in a table cell: between its {placeholders}, its ' / ' alternatives and its '...'."""
    pieces = re.split(r"\{[^}]*\}|\(not\)| / |\.\.\.|<name>", text)
    return [" ".join(p.split()) for p in pieces if len(" ".join(p.split())) >= 24]


SOURCE = _source_text()


@pytest.mark.parametrize("row", CHANGED, ids=lambda r: r["name"])
def test_no_old_text_remains(row):
    kept = " ".join(row["new"].split())
    for run in _runs(row["old"]):
        if run in kept:  # words the new text keeps
            continue
        assert run not in SOURCE, f"{row['name']} still says: {run}"


@pytest.mark.parametrize("row", CHANGED, ids=lambda r: r["name"])
def test_the_new_text_is_there(row):
    """A run of the new words is in the source. A composed sentence breaks at its plurals and its optional parts, so this
    asks for one clause of each row; tests/test_delineation.py asserts the sentences whole."""
    pieces = re.split(r"\{[^}]*\}|\(not\)| / |\.\.\.|<name>|[.,:;()]", row["new"])
    clauses = [" ".join(p.split()) for p in pieces if len(" ".join(p.split())) >= 10]
    assert not clauses or any(c in SOURCE for c in clauses), f"{row['name']} says none of: {clauses}"


def test_nothing_mentions_print():
    assert "print" not in SOURCE.lower()
    for name in ("delineation.py",):
        assert not re.search(r"\bprints?\b", (SRC / name).read_text())
