"""What the customer's agent reads in tools/list: every tool description and argument help.

Some clients (ChatGPT, openai-mcp/1.0.0) never pass the MCP server instructions to the model, so the
tool descriptions and argument help are all the agent sees. These tests read the LIVE tools/list.
"""

import ast
import json
import pathlib

import pytest

import hydrata_mcp
from tests.helpers import KEY_HEADERS, _asgi_client, _parse, _rpc

pytestmark = pytest.mark.asyncio


async def _tools(srv):
    async with _asgi_client(srv) as c:
        resp = await c.post("/", headers=KEY_HEADERS, json=_rpc("tools/list"))
    return {t["name"]: t for t in _parse(resp)["result"]["tools"]}


def _argument_help(tool, argument):
    return tool["inputSchema"]["properties"][argument].get("description", "")


# TASK-4289: no internal task id in anything the customer's agent reads (epic 4117 AC7).
async def test_no_tool_description_or_argument_help_carries_an_internal_task_id(_server):
    tools = await _tools(_server)
    leaks = []
    for name, tool in sorted(tools.items()):
        shown = {
            "description": tool.get("description", ""),
            "title": tool.get("title", ""),
            "inputSchema": json.dumps(tool.get("inputSchema", {}), ensure_ascii=False),
            "annotations": json.dumps(tool.get("annotations", {}), ensure_ascii=False),
        }
        leaks += [(name, field) for field, text in shown.items() if "TASK-" in text]
    assert not leaks, leaks


# TASK-4289: an agent should never learn a sources limit by failing; the help names the validator's limits.
@pytest.mark.parametrize(
    "tool_name", ["create_time_series", "update_time_series", "write_input_layer", "create_scenario"]
)
async def test_sources_help_states_the_limits_the_validator_enforces(_server, tool_name):
    from hydrata_mcp._helpers import SOURCES_MAX

    help_text = _argument_help((await _tools(_server))[tool_name], "sources")
    assert f"At most {SOURCES_MAX} entries" in help_text
    assert "title (1-200 characters)" in help_text
    assert "locator" in help_text and "at most 100 characters" in help_text
    assert "url (http or https, at most 2000 characters)" in help_text


async def test_sources_help_limits_match_the_validator_at_its_edges(_server):
    """The stated limits are the enforced ones: 200/100/20 pass, 201/101/21 are refused."""
    from fastmcp.exceptions import ToolError

    from hydrata_mcp._helpers import SOURCES_MAX, _clean_sources

    ok = {"title": "t" * 200, "url": "https://example.com/" + "u" * 1970, "locator": "l" * 100}
    assert _clean_sources([ok] * SOURCES_MAX)
    for bad in (
        [ok] * (SOURCES_MAX + 1),
        [{**ok, "title": "t" * 201}],
        [{**ok, "locator": "l" * 101}],
        [{**ok, "url": "https://example.com/" + "u" * 1990}],
    ):
        with pytest.raises(ToolError):
            _clean_sources(bad)


# TASK-4294: the engine (run_anuga run_utils._merge_timeseries) holds each rainfall / flow value until the next
# row (a step, no interpolation), so the help says so and agents stop inventing 1-second step rows.
@pytest.mark.parametrize("tool_name", ["create_time_series", "update_time_series"])
async def test_time_series_data_help_states_the_engine_step_rule(_server, tool_name):
    help_text = " ".join(_argument_help((await _tools(_server))[tool_name], "data").split())
    assert "each value holds from its timestamp until the next row's" in help_text
    assert "no straight line between rows" in help_text
    assert "ONE row per interval, at its START" in help_text
    assert "end with a 0 row at the end of the last interval" in help_text
    assert "never add a row just before the next one" in help_text
    assert "A stage series is the exception" in help_text


# TASK-4289 AC3: the two breakline refusal texts carry no internal task id.
async def test_breakline_refusals_carry_no_internal_task_id(_server):
    from fastmcp.exceptions import ToolError

    from hydrata_mcp.inputs import UNSUPPORTED_INPUT_LAYER_REASON

    assert "TASK-" not in UNSUPPORTED_INPUT_LAYER_REASON
    with pytest.raises(ToolError) as refused:
        await _server.attach_input_layer(project_id=42, kind="not-a-kind", execution_id="x")
    assert "breakline is refused" in str(refused.value) and "TASK-" not in str(refused.value)


# TASK-4303: tools/list is not all the agent reads. A refused call's ToolError text and the say_to_user line it is
# told to relay reach the agent (and, relayed, the person) too, so neither may carry an internal task id. The texts are
# read from the package source: every ToolError(...) argument, every say_to_user value (a dict key, an item assignment
# or a keyword), the local `say` it is built in, and what those expressions pull in -- module-level text constants,
# helper functions of the package (their body, not their docstring), and the call-site arguments of a helper whose
# parameter becomes the text (_hand_back's say_to_user).
_SAY_NAMES = {"say", "say_to_user"}


def _is_tool_error(call):
    func = call.func
    return (isinstance(func, ast.Name) and func.id == "ToolError") or (
        isinstance(func, ast.Attribute) and func.attr == "ToolError"
    )


def _call_name(call):
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _text_roots(tree):
    """(expression, enclosing function or None) for each place a customer-facing text is built."""
    roots = []

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node
        if isinstance(node, ast.Call):
            if _is_tool_error(node):
                roots.extend((arg, function) for arg in [*node.args, *(k.value for k in node.keywords)])
            roots.extend((k.value, function) for k in node.keywords if k.arg == "say_to_user")
        elif isinstance(node, ast.Dict):
            roots.extend(
                (value, function)
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and key.value == "say_to_user"
            )
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                is_say_item = (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "say_to_user"
                )
                if is_say_item or (isinstance(target, ast.Name) and target.id in _SAY_NAMES):
                    roots.append((node.value, function))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, None)
    return roots


def _customer_texts(sources):
    """[(module, line, text)] for every string an agent can read in a ToolError or a say_to_user, from `sources`
    ({module name: source text})."""
    trees = {name: ast.parse(source) for name, source in sources.items()}
    constants, functions = {}, {}
    for name, tree in trees.items():
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants.setdefault(target.id, []).append((name, node.value))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.setdefault(node.name, []).append((name, node))
    texts, seen, sinks = {}, set(), set()

    def collect(module, expr, function):
        if id(expr) in seen:
            return
        seen.add(id(expr))
        params = {a.arg for a in function.args.args + function.args.kwonlyargs} if function else set()
        for node in ast.walk(expr):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                # id(node) in the key: before Python 3.12 every literal part of one f-string carries the
                # f-string's own line/col, so a (module, line, col) key let the last part overwrite the others
                texts[(module, node.lineno, node.col_offset, id(node))] = node.value
            elif isinstance(node, ast.Name):
                if node.id in params and function.name in functions:  # a module-level helper's parameter
                    sinks.add((function.name, node.id))
                for const_module, value in constants.get(node.id, []):
                    collect(const_module, value, None)
            elif isinstance(node, ast.Call) and _call_name(node) in functions:
                for fn_module, fn in functions[_call_name(node)]:
                    body = fn.body[1:] if ast.get_docstring(fn) else fn.body
                    for statement in body:
                        collect(fn_module, statement, fn)

    for name, tree in trees.items():
        for expr, function in _text_roots(tree):
            collect(name, expr, function)
    # a helper whose parameter becomes the text: what every caller passes for it is a text too (to a fixpoint, so a
    # wrapper that hands its own parameter on to such a helper is followed as well)
    calls = []

    def find_calls(module, node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node
        if isinstance(node, ast.Call) and _call_name(node) in functions:
            calls.append((module, node, function))
        for child in ast.iter_child_nodes(node):
            find_calls(module, child, function)

    for name, tree in trees.items():
        find_calls(name, tree, None)
    done = set()
    while sinks - done:
        for fn_name, param in sinks - done:
            done.add((fn_name, param))
            [(_, fn)] = functions[fn_name][:1]
            positions = [a.arg for a in fn.args.args]
            for module, call, function in calls:
                if _call_name(call) != fn_name:
                    continue
                passed = [k.value for k in call.keywords if k.arg == param]
                if param in positions and positions.index(param) < len(call.args):
                    passed.append(call.args[positions.index(param)])
                for expr in passed:
                    collect(module, expr, function)
    return sorted((module, line, text) for (module, line, _, _), text in texts.items())


def _package_sources():
    package = pathlib.Path(hydrata_mcp.__file__).parent
    return {path.stem: path.read_text() for path in sorted(package.glob("*.py"))}


async def test_no_tool_error_or_say_to_user_text_carries_an_internal_task_id():
    texts = _customer_texts(_package_sources())
    leaks = [found for found in texts if "TASK-" in found[2]]
    assert not leaks, leaks
    # the reader is not vacuous: it reaches the texts it is meant to
    flat = " ".join(text for _, _, text in texts)
    assert len(texts) > 150, len(texts)
    assert "must be an ISO 8601 string" in flat  # a ToolError text (inputs._validate_row_data)
    assert "Nothing was sent." in flat  # the sources / terrain refusals
    assert "(No map link could be composed for project" in flat  # say_to_user built in _hand_back


async def test_the_customer_text_reader_finds_a_task_id_in_each_place_a_text_is_built():
    planted = {
        "mod": (
            "HINT = 'see TASK-1 for why'\n"
            "def _line(run):\n"
            "    '''docstring TASK-0 is not a text'''\n"
            "    return f'run {run} TASK-2'\n"
            "def _hand(result, say_to_user):\n"
            "    result['say_to_user'] = say_to_user\n"
            "def _wrap(result, line):\n"
            "    return _hand(result, line)\n"
            "def tool(x):\n"
            "    # comment TASK-9 is not a text\n"
            "    if x:\n"
            "        raise ToolError(f'bad {x}: ' + HINT)\n"
            "    say = 'plain TASK-3'\n"
            "    result = {'say_to_user': _line(x)}\n"
            "    result['say_to_user'] += ' TASK-4'\n"
            "    _hand(result, 'relayed TASK-5')\n"
            "    _wrap(result, 'wrapped TASK-8')\n"
            "    return dict(say_to_user='kw TASK-6', other='not a text TASK-7')\n"
        )
    }
    found = [text for _, _, text in _customer_texts(planted) if "TASK-" in text]
    ids = sorted(int(text.split("TASK-")[1][0]) for text in found)
    assert ids == [1, 2, 3, 4, 5, 6, 8], found
