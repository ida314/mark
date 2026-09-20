"""Every tool has to say, before it is ever called, whether re-running it is safe.

This is the metadata a crash is judged against. When the process dies between dispatching
a tool call and recording its result, the only thing that distinguishes "run it again" from
"never run it again without asking" is the class the tool declared in advance - the
arguments do not say, and the result is exactly what is missing.

So the failure being guarded against here is not a wrong class, which an audit can fix. It
is an *absent* one quietly reading as `read`: the unlabelled tool is by definition the one
nobody thought about, and a default would turn that silence into a promise that re-running
is free. The tool that would suffer from it is the one that sends mail.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest

from agentd.tools import builtin_fs
from agentd.tools.base import Tool, ToolContext, ToolResult, obj, tool
from agentd.tools.effects import (
    EFFECT_CLASSES,
    UNAUDITED,
    UNAUDITED_TOOLS,
    UNSAFE_WRITE,
    ToolRegistrationError,
    check_effect_class,
)
from agentd.tools.registry import Registry, build_registry


async def _handler(args: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(content="ok")


def _tool(name: str = "example", **kwargs) -> Tool:
    kwargs.setdefault("effect_class", "read")
    return Tool(name=name, description="a tool", parameters=obj(), handler=_handler, **kwargs)


def test_a_tool_cannot_be_built_without_saying_whether_rerunning_it_is_safe():
    with pytest.raises(TypeError, match="effect_class"):
        Tool(name="nameless", description="", parameters=obj(), handler=_handler)

    with pytest.raises(TypeError, match="effect_class"):

        @tool("undeclared", "a tool", obj(), tags=("fs",))
        async def undeclared(args: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(content="ok")


def test_the_effect_class_field_has_no_default_to_fall_back_to():
    """The guard against someone later adding `= "read"` to quiet a construction error."""
    field = {f.name: f for f in dataclasses.fields(Tool)}["effect_class"]
    assert field.default is dataclasses.MISSING
    assert field.default_factory is dataclasses.MISSING
    assert field.kw_only is True


def test_registration_refuses_a_tool_whose_effect_class_went_missing():
    """`effect_class` is a mutable field and not everything is built by the decorator."""
    registry = Registry()
    blanked = _tool("blanked")
    blanked.effect_class = None
    with pytest.raises(ToolRegistrationError, match="blanked"):
        registry.add(blanked)

    stub = dataclasses.make_dataclass("Stub", ["name"])(name="duck_typed")
    with pytest.raises(ToolRegistrationError, match="duck_typed"):
        registry.add(stub)

    assert registry.names() == []


def test_registration_refuses_an_effect_class_outside_the_vocabulary():
    """A near-miss spelling is a mislabel, and a mislabel is what duplicates an action."""
    for wrong in ("readonly", "Read", "write", "idempotent", ""):
        with pytest.raises(ToolRegistrationError):
            _tool("misspelt", effect_class=wrong)

    registry = Registry()
    drifted = _tool("drifted")
    drifted.effect_class = "safe"
    with pytest.raises(ToolRegistrationError, match="'safe'"):
        registry.add(drifted)
    assert registry.get("drifted") is None


def test_an_unclassified_tool_is_refused_rather_than_read():
    """The whole point: the failure is loud, never a quiet fallback to the free class."""
    for absent in (None, "", 0, object()):
        with pytest.raises(ToolRegistrationError) as excinfo:
            check_effect_class("anything", absent)
        assert "mandatory" in str(excinfo.value)
    assert check_effect_class("anything", "read") == "read"


def test_startup_fails_loudly_when_one_builtin_tool_is_unclassified(monkeypatch):
    """The exit criterion, exercised through the real assembly of the real registry."""
    unclassified = dataclasses.replace(builtin_fs.TOOLS[0])
    unclassified.effect_class = None
    monkeypatch.setattr(builtin_fs, "TOOLS", [unclassified, *builtin_fs.TOOLS[1:]])
    with pytest.raises(ToolRegistrationError, match=unclassified.name):
        build_registry()


def test_every_registered_tool_declares_an_effect_class():
    registry = build_registry()
    assert registry.names()
    for t in registry.tools.values():
        assert t.effect_class in EFFECT_CLASSES, t.name


def test_the_tools_nobody_has_judged_yet_are_listed_rather_than_assumed_safe():
    """`UNAUDITED_TOOLS` is the audit's remaining work, as data rather than as prose.

    Sessions 3c and 3d shrink it. Until then it is the difference between a tool whose
    class somebody chose and a tool that merely has one, which the value alone cannot say.
    """
    registry = build_registry()
    assert UNAUDITED is UNSAFE_WRITE  # the conservative value, not a fourth class
    assert UNAUDITED_TOOLS <= set(registry.names())
    for name in UNAUDITED_TOOLS:
        assert registry.tools[name].effect_class == UNSAFE_WRITE, name


def _classification_rows() -> dict[str, str]:
    """The tool -> class mapping written down in `docs/records/effect-classification.md`."""
    path = pathlib.Path(__file__).resolve().parents[1] / "docs/records/effect-classification.md"
    rows: dict[str, str] = {}
    for line in path.read_text().splitlines():
        cells = [c.strip().strip("`") for c in line.split("|")[1:-1]]
        if len(cells) != 3:
            continue
        name, klass = cells[0], cells[1]
        if klass not in (*EFFECT_CLASSES, "unaudited"):
            continue
        assert name not in rows, f"{name} is in the table twice"
        rows[name] = klass
    return rows


def test_every_tool_in_the_classification_table_declares_what_the_table_says():
    """The audit is a judgement, so it lives in prose - and prose drifts from the code.

    The class in `effect-classification.md` is the reason a human accepted the risk; the
    class in the source is what the ledger and Pass 4 actually act on. If those two ever
    disagree, the record stops being evidence of anything and the next auditor reads a
    justification for a decision nobody made. Neither one alone can catch that.

    `unaudited` in the table means "no ruling recorded", which is a claim about
    `UNAUDITED_TOOLS` rather than about the declared value, so it is checked both ways.
    """
    registry = build_registry()
    table = _classification_rows()
    assert set(table) == set(registry.names()), (
        "every registered tool needs a row, and every row a registered tool"
    )
    for name, klass in table.items():
        declared = registry.tools[name].effect_class
        if klass == "unaudited":
            assert name in UNAUDITED_TOOLS, f"{name} is listed unruled but is not in UNAUDITED_TOOLS"
            assert declared == UNSAFE_WRITE, name
        else:
            assert name not in UNAUDITED_TOOLS, f"{name} has a ruling but is still UNAUDITED"
            assert declared == klass, f"{name}: source says {declared}, the record says {klass}"
