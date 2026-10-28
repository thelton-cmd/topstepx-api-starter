"""AST node types for the Pine Script subset the converter understands.

Every node records the 1-based ``line`` and ``col`` of its first token. Nodes compare by
identity (``eq=False``) so the analyzer can key dictionaries on them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(eq=False)
class Node:
    line: int
    col: int


# -- expressions ---------------------------------------------------------------------------


@dataclass(eq=False)
class Num(Node):
    value: float | int
    is_int: bool


@dataclass(eq=False)
class Str(Node):
    value: str


@dataclass(eq=False)
class Bool(Node):
    value: bool


@dataclass(eq=False)
class ColorLit(Node):
    value: str               # "#rrggbb" or "#rrggbbaa", lower case


@dataclass(eq=False)
class Name(Node):
    id: str                  # may be dotted: "close", "color.red", "syminfo.mintick"


@dataclass(eq=False)
class Call(Node):
    func: str                # dotted name: "ta.sma", "plot", "myFunction"
    args: list
    kwargs: dict             # name -> expression (insertion order kept)
    generic: str | None = None   # "array.new<float>" style type argument, if any


@dataclass(eq=False)
class Index(Node):
    target: Node
    offset: Node             # history reference: target[offset]


@dataclass(eq=False)
class Unary(Node):
    op: str                  # "-", "+", "not"
    operand: Node


@dataclass(eq=False)
class Binary(Node):
    op: str                  # + - * / % == != < > <= >= and or
    left: Node
    right: Node


@dataclass(eq=False)
class Ternary(Node):
    cond: Node
    then: Node
    other: Node


@dataclass(eq=False)
class TupleExpr(Node):
    items: list              # [a, b] (a function's return value)


# -- statements (If / Switch / For / While can also be used as values) ----------------------


@dataclass(eq=False)
class VarDecl(Node):
    name: str
    value: Node
    mode: str | None = None          # None, "var" or "varip"
    type_name: str | None = None     # "float", "int", "series float", ...
    end_line: int = 0


@dataclass(eq=False)
class TupleDecl(Node):
    names: list
    value: Node
    end_line: int = 0


@dataclass(eq=False)
class Assign(Node):
    name: str
    op: str                  # ":=", "+=", "-=", "*=", "/=", "%="
    value: Node
    end_line: int = 0


@dataclass(eq=False)
class ExprStmt(Node):
    expr: Node
    end_line: int = 0


@dataclass(eq=False)
class If(Node):
    cond: Node
    body: list
    elifs: list = field(default_factory=list)    # [(cond, body), ...]
    orelse: list | None = None


@dataclass(eq=False)
class For(Node):
    var: str
    start: Node
    end: Node
    step: Node | None
    body: list


@dataclass(eq=False)
class ForIn(Node):
    names: list
    iterable: Node
    body: list


@dataclass(eq=False)
class While(Node):
    cond: Node
    body: list


@dataclass(eq=False)
class Switch(Node):
    subject: Node | None
    cases: list              # [(match expression or None for the default, body), ...]


@dataclass(eq=False)
class Param(Node):
    name: str
    default: Node | None = None
    type_name: str | None = None


@dataclass(eq=False)
class FuncDef(Node):
    name: str
    params: list
    body: list
    end_line: int = 0


@dataclass(eq=False)
class Break(Node):
    pass


@dataclass(eq=False)
class Continue(Node):
    pass


@dataclass(eq=False)
class Skipped(Node):
    """A statement the parser read but the converter does not support (import, type, method...)."""
    what: str
    end_line: int = 0


@dataclass(eq=False)
class Script(Node):
    body: list
    version: int | None = None


STRUCTURES = (If, Switch, For, While, ForIn)
