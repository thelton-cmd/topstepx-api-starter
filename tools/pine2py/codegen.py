"""Code generator: Pine AST -> a dashboard indicator plugin (Python source text).

The generated module follows the plugin contract in ``dashboard/indicator_plugins.py`` and runs the
script once per bar through ``tools.pine2py.runtime``. How Pine's rules map to Python:

- Every ``ta.*`` call and every user function call gets its own ``State`` (``root.sub(key)`` at the
  top level, ``st.sub(key)`` inside a function), so two ``ta.sma(close, 20)`` never share history,
  also when they sit inside a function called from two places.
- A variable that is read with ``x[n]`` gets a ``Series``; ``x = e`` pushes a slot and sets it,
  ``x := e`` sets the current slot. ``var`` variables live in Python locals (``UNSET`` until
  initialised) or, inside functions, in ``st.vars``.
- Arithmetic uses the runtime's ``na`` (propagates, compares false); ``/`` and ``%`` call ``div`` and
  ``mod`` unless the divisor is a non-zero literal.
- ``plot*`` calls become output series created once before the loop and fed once per bar.

Nothing here adds rules, levels or signals: it only translates the user's own script.
"""

from __future__ import annotations

import dataclasses
import keyword
import re
import builtins as py_builtins

from . import builtins as B
from . import nodes as N
from . import runtime as rt
from .errors import PineSyntaxError

# -- naming -------------------------------------------------------------------------------------

_CODEGEN_NAMES = {"ctx", "bar", "out", "root", "params", "candles", "st", "compute", "NAME", "TITLE", "DESCRIPTION",
                  "OVERLAY", "PARAMS", "rt", "tools", "dashboard"}
RESERVED = (set(keyword.kwlist) | set(dir(py_builtins)) | {n for n in dir(rt) if not n.startswith("__")}
            | _CODEGEN_NAMES | set(B.COLUMNS) | {"na"})
_PREC_TERNARY, _PREC_OR, _PREC_AND, _PREC_NOT, _PREC_CMP, _PREC_ADD, _PREC_MUL, _PREC_UNARY, _PREC_ATOM = range(1, 10)
MARKER_POSITIONS = ("aboveBar", "belowBar")
INPUT_ORDER = {
    "input.int": ["defval", "title", "minval", "maxval", "step", "tooltip", "inline", "group", "confirm"],
    "input.float": ["defval", "title", "minval", "maxval", "step", "tooltip", "inline", "group", "confirm"],
    "input.bool": ["defval", "title", "tooltip", "inline", "group", "confirm"],
    "input.string": ["defval", "title", "options", "tooltip", "inline", "group", "confirm"],
    "input.source": ["defval", "title", "tooltip", "inline", "group"],
    "input.color": ["defval", "title", "tooltip", "inline", "group", "confirm"],
    "input": ["defval", "title", "type", "minval", "maxval", "step", "options", "tooltip", "inline", "group", "confirm"],
}
DRAWING_NAMESPACES = ("label.", "line.", "box.", "table.", "linefill.", "polyline.", "chart.")
SKIPPED_CALLS = {"max_bars_back", "alert", "runtime.error", "log.info", "log.warning", "log.error"}


def pyname(name: str) -> str:
    return name + "_" if name in RESERVED or name.startswith("_") else name


def slug(text: str, fallback: str = "x") -> str:
    out = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    if not out:
        out = fallback
    if not out[0].isalpha():
        out = fallback + "_" + out
    return out[:40]


def ascii_repr(value) -> str:
    return ascii(value)


class _NotConst(Exception):
    pass


@dataclasses.dataclass
class VarInfo:
    name: str
    code: str                      # python expression that reads the current value
    kind: str = "var"              # var | input | column | func
    store: str | None = None       # assignment target ("x" or "_v.x"); None = cannot be assigned
    hist: str | None = None        # python name of its Series
    col: str | None = None         # for source inputs: python name of the column object
    static: bool = False           # an input: constant over the whole run
    node: object = None


class Scope:
    def __init__(self, kind: str, parent: "Scope | None" = None):
        self.kind = kind            # "global" or "func"
        self.parent = parent
        self.vars: dict[str, VarInfo] = {}
        self.prologue: list[str] = []
        self.state_base = "root" if kind == "global" else "st"
        self.uses_vars = False

    def lookup(self, name):
        scope = self
        while scope is not None:
            info = scope.vars.get(name)
            if info is not None:
                return info
            scope = scope.parent
        return None


def _walk(node):
    if isinstance(node, N.Node):
        yield node
        for f in dataclasses.fields(node):
            yield from _walk(getattr(node, f.name))
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        for item in node.values():
            yield from _walk(item)


# -- the generator ----------------------------------------------------------------------------------


class Generator:
    def __init__(self, script: N.Script, version: int | None, name: str | None):
        self.script = script
        self.version = version
        self.name_hint = name
        self.warnings: list[str] = []
        self.params: list[dict] = []
        self.param_lines: list[str] = []
        self.plot_decls: list[str] = []
        self.global_prologue: list[str] = []
        self.func_lines: list[str] = []
        self.lines: list[str] = []
        self.ind = 2                       # body of the for loop inside compute
        self.counter = 0
        self.title = None
        self.overlay = False
        self.is_strategy = False
        self.global_scope = Scope("global")
        self.scope = self.global_scope
        self.funcs: dict[str, N.FuncDef] = {}
        self.indexed: set[str] = set()
        self.used_ids: set[str] = set()
        self.strategy_series: dict[tuple, str] = {}
        self.uses_mintick = False
        self.input_count = 0
        self.plot_count = 0

    # -- helpers ---------------------------------------------------------------------------------

    def warn(self, message: str, node=None):
        text = f"line {node.line}: {message}" if node is not None and getattr(node, "line", 0) else message
        if text not in self.warnings:
            self.warnings.append(text)

    def error(self, message: str, node=None):
        raise PineSyntaxError(message, getattr(node, "line", 0), getattr(node, "col", 0))

    def emit(self, text: str):
        self.lines.append("    " * self.ind + text)

    def unique(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def unique_id(self, base: str) -> str:
        base = slug(base, "plot")
        out, k = base, 2
        while out in self.used_ids:
            out = f"{base}_{k}"
            k += 1
        self.used_ids.add(out)
        return out

    def new_state(self, hint: str) -> str:
        self.counter += 1
        hint = slug(hint, "site").replace(".", "_")
        var = f"{hint}_{self.counter}"
        self.scope.prologue.append(f"{var} = {self.scope.state_base}.sub({var!r})")
        return var

    def new_hist(self) -> str:
        var = self.unique("h")
        if self.scope.kind == "global":
            self.scope.prologue.append(f"{var} = Series()")
        else:
            self.scope.prologue.append(f"{var} = st.series({var!r})")
        return var

    def lookup(self, name: str):
        return self.scope.lookup(name)

    # -- const evaluation ----------------------------------------------------------------------------

    def const(self, n):
        if isinstance(n, (N.Num, N.Str, N.Bool, N.ColorLit)):
            return n.value
        if isinstance(n, N.Name):
            if self.lookup(n.id) is not None:
                raise _NotConst
            key = self.resolve_alias(n.id)
            if key in B.CONSTANTS:
                return B.CONSTANTS[key]
            raise _NotConst
        if isinstance(n, N.Unary) and n.op in "+-":
            v = self.const(n.operand)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return -v if n.op == "-" else v
            raise _NotConst
        if isinstance(n, N.Binary) and n.op in ("+", "-", "*", "/"):
            a, b = self.const(n.left), self.const(n.right)
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (a, b)):
                if n.op == "/":
                    if b == 0:
                        raise _NotConst
                    return a / b
                return {"+": a + b, "-": a - b, "*": a * b}[n.op]
            if n.op == "+" and isinstance(a, str) and isinstance(b, str):
                return a + b
            raise _NotConst
        if isinstance(n, N.TupleExpr):
            return [self.const(i) for i in n.items]
        if isinstance(n, N.Call) and n.func in ("color.new", "color.rgb") and not n.kwargs:
            vals = [self.const(a) for a in n.args]
            try:
                return rt.color_new(*vals) if n.func == "color.new" else rt.color_rgb(*vals)
            except TypeError:
                raise _NotConst from None
        raise _NotConst

    def try_const(self, n, default=None):
        if n is None:
            return default
        try:
            return self.const(n)
        except _NotConst:
            return default

    def resolve_alias(self, name: str) -> str:
        if name in B.V4_VARIABLES and name not in B.CONSTANTS:
            return B.V4_VARIABLES[name]
        return name

    def is_static(self, n) -> bool:
        if isinstance(n, (N.Num, N.Str, N.Bool, N.ColorLit)):
            return True
        if isinstance(n, N.Name):
            info = self.lookup(n.id)
            if info is not None:
                return info.kind == "input"
            return self.resolve_alias(n.id) in B.CONSTANTS
        if isinstance(n, N.Unary):
            return self.is_static(n.operand)
        if isinstance(n, N.Binary):
            return self.is_static(n.left) and self.is_static(n.right)
        if isinstance(n, N.Ternary):
            return all(self.is_static(x) for x in (n.cond, n.then, n.other))
        if isinstance(n, N.TupleExpr):
            return all(self.is_static(x) for x in n.items)
        if isinstance(n, N.Call):
            fn = B.FUNCS.get(n.func)
            pure = n.func in ("color.new", "color.rgb") or (fn is not None and fn.first is None and n.func.startswith("math."))
            return pure and all(self.is_static(a) for a in list(n.args) + list(n.kwargs.values()))
        return False

    # -- expressions --------------------------------------------------------------------------------------

    def E(self, n, need: int = 0) -> str:
        code, prec = self.expr(n)
        return f"({code})" if prec < need else code

    def expr(self, n) -> tuple[str, int]:
        if isinstance(n, N.Num):
            return repr(n.value), _PREC_ATOM
        if isinstance(n, N.Str):
            return ascii_repr(n.value), _PREC_ATOM
        if isinstance(n, N.Bool):
            return ("True" if n.value else "False"), _PREC_ATOM
        if isinstance(n, N.ColorLit):
            return ascii_repr(n.value), _PREC_ATOM
        if isinstance(n, N.Name):
            return self.name_expr(n)
        if isinstance(n, N.Call):
            return self.call_expr(n)
        if isinstance(n, N.Index):
            return self.index_expr(n), _PREC_ATOM
        if isinstance(n, N.Unary):
            if n.op == "not":
                return f"not {self.E(n.operand, _PREC_ADD)}", _PREC_NOT
            if n.op == "+":
                return self.expr(n.operand)
            if isinstance(n.operand, N.Num):
                return repr(-n.operand.value), _PREC_ATOM
            return f"-{self.E(n.operand, _PREC_UNARY)}", _PREC_UNARY
        if isinstance(n, N.Binary):
            return self.binary_expr(n)
        if isinstance(n, N.Ternary):
            return (f"{self.E(n.then, _PREC_OR)} if {self.E(n.cond, _PREC_OR)} else {self.E(n.other, _PREC_TERNARY)}",
                    _PREC_TERNARY)
        if isinstance(n, N.TupleExpr):
            items = [self.E(i) for i in n.items]
            return "(" + ", ".join(items) + ("," if len(items) == 1 else "") + ")", _PREC_ATOM
        if isinstance(n, N.STRUCTURES):
            self.error("an if/for/while/switch can only be used as a statement or as the value of a declaration", n)
        self.error(f"cannot convert {type(n).__name__}", n)

    def binary_expr(self, n):
        op = n.op
        if op == "/":
            if isinstance(n.right, N.Num) and n.right.value != 0:
                return f"{self.E(n.left, _PREC_MUL)} / {self.E(n.right, _PREC_UNARY)}", _PREC_MUL
            return f"div({self.E(n.left)}, {self.E(n.right)})", _PREC_ATOM
        if op == "%":
            return f"mod({self.E(n.left)}, {self.E(n.right)})", _PREC_ATOM
        if op in ("+", "-"):
            return f"{self.E(n.left, _PREC_ADD)} {op} {self.E(n.right, _PREC_MUL)}", _PREC_ADD
        if op == "*":
            return f"{self.E(n.left, _PREC_MUL)} * {self.E(n.right, _PREC_UNARY)}", _PREC_MUL
        if op in ("<", ">", "<=", ">=", "==", "!="):
            return f"{self.E(n.left, _PREC_ADD)} {op} {self.E(n.right, _PREC_ADD)}", _PREC_CMP
        if op == "and":
            return f"{self.E(n.left, _PREC_AND)} and {self.E(n.right, _PREC_NOT)}", _PREC_AND
        if op == "or":
            return f"{self.E(n.left, _PREC_OR)} or {self.E(n.right, _PREC_AND)}", _PREC_OR
        self.error(f"unsupported operator {op}", n)

    def name_expr(self, n: N.Name):
        info = self.lookup(n.id)
        if info is not None:
            return info.code, _PREC_ATOM
        key = self.resolve_alias(n.id)
        if key == "na":
            return "na", _PREC_ATOM
        if key in B.COLUMNS:
            return f"bar.{B.COLUMNS[key]}", _PREC_ATOM
        if key in B.VARIABLES:
            code, _kind, note = B.VARIABLES[key]
            if note:
                self.warn(note, n)
            if key == "syminfo.mintick":
                self.use_mintick()
                return "tick_size", _PREC_ATOM
            code = re.sub(r"\brt\.", "", code)
            code = re.sub(r"(?<![\w.])time\b", "bar.time", code)
            return code, _PREC_ATOM
        if key in B.STATE_VARIABLES:
            py, args = B.STATE_VARIABLES[key]
            state = self.new_state(py)
            return f"{py}({', '.join([state] + [f'bar.{a}' for a in args])})", _PREC_ATOM
        if key in B.CTX_VARIABLES:
            return re.sub(r"\brt\.", "", B.CTX_VARIABLES[key]), _PREC_ATOM
        if key in B.CONSTANTS:
            value = B.CONSTANTS[key]
            return (repr(value) if not isinstance(value, str) else ascii_repr(value)), _PREC_ATOM
        head = n.id.split(".", 1)[0]
        if head in B.UNSUPPORTED_NAMESPACES and "." in n.id:
            self.error(f"{n.id} is not supported ({B.UNSUPPORTED_NAMESPACES[head]})", n)
        self.error(f"'{n.id}' is not defined", n)

    def use_mintick(self):
        if not any(p["name"] == "tick_size" for p in self.params):
            self.params.append({"name": "tick_size", "label": "Tick size (syminfo.mintick)", "type": "float",
                                "default": rt.DEFAULT_TICK, "min": 0.000001,
                                "help": "Pine's syminfo.mintick; the dashboard does not pass the symbol's tick size"})
            self.param_lines.append('tick_size = params["tick_size"]')
            self.warn("syminfo.mintick is the 'tick_size' setting (default 0.25) because dashboard indicators do not know the symbol")

    def index_expr(self, n: N.Index) -> str:
        off = self.E(n.offset)
        zero = isinstance(n.offset, N.Num) and n.offset.value == 0
        t = n.target
        if isinstance(t, N.Name):
            info = self.lookup(t.id)
            if info is not None:
                if info.kind == "input":
                    return info.code
                if info.kind == "column":
                    return f"{info.col}[{off}]"
                if info.hist:
                    return f"{info.hist}[{off}]"
            else:
                key = self.resolve_alias(t.id)
                if key in B.COLUMNS:
                    return f"bar.{B.COLUMNS[key]}" if zero else f"ctx.{key}[{off}]"
                if key in B.CONSTANTS:
                    return self.E(t)
        if zero:
            return self.E(t, _PREC_ATOM)
        return f"hist({self.new_hist()}, {self.E(t)}, {off})"

    # -- calls ---------------------------------------------------------------------------------------------

    def bind(self, c: N.Call, names: list, what: str | None = None) -> dict:
        what = what or c.func
        got: dict = {}
        if len(c.args) > len(names):
            self.error(f"{what}() takes at most {len(names)} arguments, {len(c.args)} given", c)
        for name, arg in zip(names, c.args):
            got[name] = arg
        for k, v in c.kwargs.items():
            if k in got:
                self.error(f"argument '{k}' of {what}() is given twice", v)
            got[k] = v
        return got

    def call_expr(self, c: N.Call):
        f = c.func
        if f in self.funcs:
            return self.user_call(c), _PREC_ATOM
        canon = f
        if f not in B.FUNCS and f in B.V4_FUNCS and f not in ("na", "nz"):
            canon = B.V4_FUNCS[f]
        if canon == "na":
            args = self.positional(c, 1, 1)
            return f"isna({self.E(args[0])})", _PREC_ATOM
        if canon == "nz":
            args = self.positional(c, 1, 2)
            return f"nz({', '.join(self.E(a) for a in args)})", _PREC_ATOM
        if canon == "iff":
            args = self.positional(c, 3, 3)
            return N_ternary(self, args), _PREC_TERNARY
        if canon in B.INPUT_FUNCS or f in B.INPUT_FUNCS:
            return self.inline_input(c), _PREC_ATOM
        if canon == "ta.tr":
            args = self.positional(c, 0, 1)
            return f"ta_tr(ctx, {self.E(args[0]) if args else 'False'})", _PREC_ATOM
        if canon in B.FUNCS:
            return self.builtin_call(c, canon), _PREC_ATOM
        if canon in ("indicator", "study", "strategy", "library"):
            self.error(f"{canon}() must be the first statement of the script", c)
        if canon in B.PLOT_FUNCS or canon in ("alertcondition", "bgcolor", "barcolor", "fill") or canon.startswith("strategy."):
            self.error(f"{canon}() cannot be used inside an expression", c)
        if canon.startswith(DRAWING_NAMESPACES):
            self.warn(f"{canon}() draws objects on the chart, which dashboard indicators cannot do; its value is na", c)
            return "na", _PREC_ATOM
        if canon in B.UNSUPPORTED_FUNCS and B.UNSUPPORTED_FUNCS[canon]:
            self.error(B.UNSUPPORTED_FUNCS[canon], c)
        head = canon.split(".", 1)[0]
        if "." in canon and head in B.UNSUPPORTED_NAMESPACES:
            self.error(f"{canon}() is not supported ({B.UNSUPPORTED_NAMESPACES[head]})", c)
        self.error(f"unknown function '{canon}'", c)

    def positional(self, c: N.Call, lo: int, hi: int) -> list:
        if c.kwargs or not lo <= len(c.args) <= hi:
            self.error(f"{c.func}() takes {lo if lo == hi else f'{lo} to {hi}'} positional argument(s)", c)
        return c.args

    def builtin_call(self, c: N.Call, canon: str) -> str:
        fn = B.FUNCS[canon]
        if fn.note:
            self.warn(fn.note, c)
        if canon in ("math.round_to_mintick",):
            self.use_mintick()
        py = fn.py
        if fn.variadic:
            if c.kwargs:
                self.error(f"{canon}() takes only positional arguments", c)
            return f"{py}({', '.join(self.E(a) for a in c.args)})"
        names = list(fn.params)
        got = None
        nargs = len(c.args) + len(c.kwargs)
        if canon in B.OVERLOAD_SOURCE and nargs == 1 and "source" not in c.kwargs:
            only = self.bind(c, ["length"], canon)
            got = {"source": f"bar.{B.OVERLOAD_SOURCE[canon]}", "length": only["length"]}
        elif canon in B.PIVOT_SOURCE and nargs == 2 and "source" not in c.kwargs:
            only = self.bind(c, ["leftbars", "rightbars"], canon)
            got = {"source": f"bar.{B.PIVOT_SOURCE[canon]}", **only}
        if got is None:
            got = self.bind(c, names, canon)
        unknown = set(got) - set(names)
        if unknown:
            self.error(f"{canon}() has no argument named '{sorted(unknown)[0]}'", c)
        codes: list = []
        for p in names:
            node = got.get(p)
            if isinstance(node, str):
                codes.append(node)
            elif node is not None:
                codes.append(self.E(node))
            elif p in fn.defaults:
                d = fn.defaults[p]
                codes.append(None if d is B.OMIT else d)
            else:
                self.error(f"{canon}() is missing its '{p}' argument", c)
        while codes and codes[-1] is None:
            codes.pop()
        codes = ["None" if x is None else x for x in codes]
        prefix: list = []
        if fn.first == "state":
            prefix = [self.new_state(py.split("_", 1)[-1])]
        elif fn.first == "ctx":
            prefix = ["ctx"]
        return f"{py}({', '.join(prefix + codes)})"

    def user_call(self, c: N.Call) -> str:
        fd = self.funcs[c.func]
        names = [p.name for p in fd.params]
        got = self.bind(c, names, c.func)
        unknown = set(got) - set(names)
        if unknown:
            self.error(f"{c.func}() has no argument named '{sorted(unknown)[0]}'", c)
        args = []
        for p in fd.params:
            node = got.get(p.name) or p.default
            if node is None:
                self.error(f"{c.func}() is missing its '{p.name}' argument", c)
            args.append(self.E(node))
        state = self.new_state(c.func)
        return f"{self.func_py(fd.name)}({', '.join([state] + args)})"

    def func_py(self, name: str) -> str:
        return "f_" + name

    # -- inputs ---------------------------------------------------------------------------------------------

    def inline_input(self, c: N.Call) -> str:
        return self.make_input(c, None)[0]

    def make_input(self, c: N.Call, var_name: str | None):
        """Register the parameter; returns (value code, VarInfo kwargs)."""
        if self.scope.kind != "global":
            self.error("input() can only be used at the top level of the script, not inside a function", c)
        func = c.func
        order = INPUT_ORDER.get(func)
        if order is None:
            self.warn(f"{func}() is not supported by the dashboard; its default value is used", c)
            order = INPUT_ORDER["input"]
        got = self.bind(c, order, func)
        defnode = got.get("defval")
        if defnode is None:
            self.error(f"{func}() needs a default value", c)
        kind = {"input.int": "int", "input.float": "float", "input.bool": "bool", "input.string": "str",
                "input.source": "source", "input.color": "color"}.get(func)
        if kind is None and func == "input":
            tnode = got.get("type")
            tname = tnode.id if isinstance(tnode, N.Name) else None
            tname = B.V4_ALIASES.get(tname, tname)
            kind = {"input.int": "int", "input.float": "float", "input.bool": "bool", "input.string": "str",
                    "input.source": "source", "input.color": "color"}.get(tname)
            if kind is None:
                if isinstance(defnode, N.Bool):
                    kind = "bool"
                elif isinstance(defnode, N.Num):
                    kind = "int" if defnode.is_int else "float"
                elif isinstance(defnode, N.Str):
                    kind = "str"
                elif isinstance(defnode, N.ColorLit) or (isinstance(defnode, N.Name) and self.resolve_alias(defnode.id).startswith("color.")):
                    kind = "color"
                elif isinstance(defnode, N.Name) and defnode.id in B.SOURCE_CHOICES:
                    kind = "source"
                else:
                    kind = "str"
        if kind is None:
            kind = "str"
            self.warn(f"{func}() is read as a text setting with its default value", c)
        self.input_count += 1
        pname = var_name or f"input_{self.input_count}"
        if any(p["name"] == pname for p in self.params):
            pname = f"{pname}_{self.input_count}"
        title = self.try_const(got.get("title"))
        label = str(title) if isinstance(title, str) and title else (var_name or pname)
        spec: dict = {"name": pname, "label": label}
        py_var = pyname(pname) if var_name else None
        access = f"params[{pname!r}]"
        value_code = access
        info: dict = {"kind": "input", "static": True}
        if kind == "source":
            d = defnode.id if isinstance(defnode, N.Name) else None
            d = self.resolve_alias(d) if d else d
            if d not in B.SOURCE_CHOICES:
                self.error("the default of input.source must be a price series such as close or hl2", defnode)
            spec.update(type="str", default=d, choices=list(B.SOURCE_CHOICES))
            col = f"{py_var or pname}_col"
            self.param_lines.append(f"{col} = ctx.source({access})")
            value_code = f"{col}[0]"
            info.update(kind="column", col=col)
            py_var = None
        else:
            dv = self.try_const(defnode, _NotConst)
            if dv is _NotConst:
                self.error(f"the default value of {func}() must be a constant", defnode)
            if kind == "color":
                if not isinstance(dv, str) or not dv.startswith("#"):
                    self.error("the default of input.color must be a color", defnode)
                alpha = dv[7:9] if len(dv) == 9 else ""
                spec.update(type="str", default=dv[:7], format="color")
                value_code = f"color_param({access}, {dv[:7]!r})"
                if alpha:
                    value_code = f"color_with_alpha({value_code}, {alpha!r})"
            elif kind == "bool":
                spec.update(type="bool", default=bool(dv))
            elif kind in ("int", "float"):
                if not isinstance(dv, (int, float)) or isinstance(dv, bool):
                    self.error(f"the default of {func}() must be a number", defnode)
                spec.update(type=kind, default=int(dv) if kind == "int" else float(dv))
                for key, pk in (("minval", "min"), ("maxval", "max"), ("step", "step")):
                    if key in got:
                        v = self.try_const(got[key], _NotConst)
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            spec[pk] = v
                        else:
                            self.warn(f"{key} of {func}() is not a constant and is ignored", got[key])
                opts = self.try_const(got.get("options"))
                if isinstance(opts, list) and opts:
                    spec.update(type="str", default=str(spec["default"]), choices=[str(o) for o in opts])
                    spec.pop("min", None), spec.pop("max", None), spec.pop("step", None)
                    value_code = f"{'int' if kind == 'int' else 'float'}({access})"
                    if str(spec["default"]) not in spec["choices"]:
                        spec["choices"].insert(0, str(spec["default"]))
            else:
                if not isinstance(dv, str):
                    self.error(f"the default of {func}() must be text", defnode)
                spec.update(type="str", default=dv)
                opts = self.try_const(got.get("options"))
                if isinstance(opts, list) and opts and all(isinstance(o, str) for o in opts):
                    spec["choices"] = list(opts)
                    if dv not in opts:
                        spec["choices"].insert(0, dv)
        tip = self.try_const(got.get("tooltip"))
        if isinstance(tip, str) and tip:
            spec["help"] = tip
        self.params.append(spec)
        if py_var:
            self.param_lines.append(f"{py_var} = {value_code}")
            info["code"] = py_var
            return py_var, info
        if kind == "source":
            info["code"] = value_code
            return value_code, info
        info["code"] = value_code
        return value_code, info

    # -- statements ------------------------------------------------------------------------------------------

    def gen_body(self, body: list, target: str | None = None):
        start = len(self.lines)
        for i, stmt in enumerate(body):
            if target is not None and i == len(body) - 1:
                self.gen_value_stmt(stmt, target)
            else:
                self.gen_stmt(stmt)
        if len(self.lines) == start:
            self.emit("pass")

    def gen_value_stmt(self, stmt, target: str):
        if isinstance(stmt, N.ExprStmt):
            if isinstance(stmt.expr, N.Call) and self.is_statement_call(stmt.expr):
                self.gen_stmt(stmt)
                self.emit(f"{target} = na")
            else:
                self.emit(f"{target} = {self.E(stmt.expr)}")
        elif isinstance(stmt, N.If):
            self.gen_if(stmt, target)
        elif isinstance(stmt, N.Switch):
            self.gen_switch(stmt, target)
        elif isinstance(stmt, (N.For, N.While)):
            self.emit(f"{target} = na")
            self.gen_loop(stmt, target)
        elif isinstance(stmt, N.VarDecl):
            self.gen_stmt(stmt)
            info = self.lookup(stmt.name)
            self.emit(f"{target} = {info.code}")
        elif isinstance(stmt, N.Assign):
            self.gen_stmt(stmt)
            info = self.lookup(stmt.name)
            self.emit(f"{target} = {info.code}")
        elif isinstance(stmt, N.TupleDecl):
            self.gen_stmt(stmt)
            self.emit(f"{target} = ({', '.join(self.lookup(n).code for n in stmt.names)})")
        else:
            self.gen_stmt(stmt)

    def gen_stmt(self, s):
        if isinstance(s, N.VarDecl):
            self.gen_decl(s)
        elif isinstance(s, N.TupleDecl):
            self.gen_tuple_decl(s)
        elif isinstance(s, N.Assign):
            self.gen_assign(s)
        elif isinstance(s, N.ExprStmt):
            self.gen_expr_stmt(s)
        elif isinstance(s, N.If):
            self.gen_if(s, None)
        elif isinstance(s, N.Switch):
            self.gen_switch(s, None)
        elif isinstance(s, (N.For, N.While)):
            self.gen_loop(s, None)
        elif isinstance(s, N.ForIn):
            self.error("for ... in loops need arrays, which are not supported", s)
        elif isinstance(s, N.Break):
            self.emit("break")
        elif isinstance(s, N.Continue):
            self.emit("continue")
        elif isinstance(s, N.FuncDef):
            self.error("functions must be defined at the top level of the script", s)
        elif isinstance(s, N.Skipped):
            self.warn(f"{s.what} is not supported and was skipped", s)
        else:
            self.error(f"cannot convert {type(s).__name__}", s)

    def declare(self, name: str, node, mode: str | None = None, forced_code: str | None = None) -> VarInfo:
        py = pyname(name)
        hist = None
        if name in self.indexed:
            hist = f"{py}_hist"
            if self.scope.kind == "global":
                if hist not in self.scope.prologue:
                    self.scope.prologue.append(f"{hist} = Series()")
            else:
                line = f"{hist} = st.series({name!r})"
                if line not in self.scope.prologue:
                    self.scope.prologue.append(line)
        if mode and self.scope.kind == "func":
            self.scope.uses_vars = True
            info = VarInfo(name, f"_v.{py}", "var", f"_v.{py}", hist, node=node)
        else:
            info = VarInfo(name, py, "var", py, hist, node=node)
        self.scope.vars[name] = info
        return info

    def gen_decl(self, s: N.VarDecl):
        v = s.value
        if isinstance(v, N.Call) and (v.func in B.INPUT_FUNCS):
            if s.mode:
                self.error("an input cannot be declared with var", s)
            code, info = self.make_input(v, s.name)
            self.scope.vars[s.name] = VarInfo(s.name, info["code"], info["kind"], None, None, info.get("col"), True, s)
            return
        if isinstance(v, N.Call) and v.func in B.PLOT_FUNCS and not s.mode:
            self.gen_plot(v)           # p1 = plot(...): the handle is only useful to fill(), which is skipped
            self.scope.vars[s.name] = VarInfo(s.name, "na", "plot", None, None, None, False, s)
            return
        if isinstance(v, N.Call) and v.func.startswith(DRAWING_NAMESPACES):
            self.warn(f"{v.func}() draws objects on the chart, which dashboard indicators cannot do; the variable is na", v)
            v = N.Name(v.line, v.col, "na")
        if s.mode:
            self.gen_var_decl(s, v)
            return
        info = self.declare(s.name, s)
        if info.hist:
            self.emit(f"{info.hist}.push(na)")
        if isinstance(v, N.STRUCTURES):
            self.emit(f"{info.store} = na")
            self.gen_structure(v, info.store)
            if info.hist:
                self.emit(f"{info.hist}.set({info.store})")
        elif info.hist:
            self.emit(f"{info.store} = {info.hist}.set({self.E(v)})")
        else:
            self.emit(f"{info.store} = {self.E(v)}")

    def gen_var_decl(self, s: N.VarDecl, v):
        info = self.declare(s.name, s, mode=s.mode)
        py = pyname(s.name)
        if self.scope.kind == "global":
            self.scope.prologue.append(f"{py} = UNSET")
            self.emit(f"if {py} is UNSET:")
            self.ind += 1
            if isinstance(v, N.STRUCTURES):
                self.emit(f"{py} = na")
                self.gen_structure(v, py)
            else:
                self.emit(f"{py} = {self.E(v)}")
            self.ind -= 1
        else:
            self.emit(f"if {s.name!r} not in _v:")
            self.ind += 1
            if isinstance(v, N.STRUCTURES):
                self.emit(f"{info.store} = na")
                self.gen_structure(v, info.store)
            else:
                self.emit(f"{info.store} = {self.E(v)}")
            self.ind -= 1
        if info.hist:
            self.emit(f"{info.hist}.push({info.store})")

    def gen_tuple_decl(self, s: N.TupleDecl):
        infos = [self.declare(name, s) for name in s.names]
        for info in infos:
            if info.hist:
                self.emit(f"{info.hist}.push(na)")
        value = s.value
        if isinstance(value, N.STRUCTURES):
            tmp = self.unique("_t")
            self.emit(f"{tmp} = na")
            self.gen_structure(value, tmp)
            rhs = tmp
        else:
            rhs = self.E(value)
        self.emit(f"{', '.join(i.store for i in infos)} = unpack({rhs}, {len(infos)})")
        for info in infos:
            if info.hist:
                self.emit(f"{info.hist}.set({info.store})")

    def gen_assign(self, s: N.Assign):
        info = self.lookup(s.name)
        if info is None:
            self.error(f"'{s.name}' is not declared (use '{s.name} = ...' to declare it first)", s)
        if info.store is None:
            self.error(f"'{s.name}' is an input and cannot be reassigned", s)
        if self.scope.kind == "func" and s.name not in self.scope.vars:
            self.error(f"a function cannot change the global variable '{s.name}'", s)
        value = s.value
        if s.op != ":=":
            value = N.Binary(s.line, s.col, s.op[0], N.Name(s.line, s.col, s.name), s.value)
        if isinstance(value, N.STRUCTURES):
            self.emit(f"{info.store} = na")
            self.gen_structure(value, info.store)
            if info.hist:
                self.emit(f"{info.hist}.set({info.store})")
        elif info.hist:
            self.emit(f"{info.store} = {info.hist}.set({self.E(value)})")
        else:
            self.emit(f"{info.store} = {self.E(value)}")

    def gen_structure(self, v, target: str):
        if isinstance(v, N.If):
            self.gen_if(v, target)
        elif isinstance(v, N.Switch):
            self.gen_switch(v, target)
        else:
            self.gen_loop(v, target)

    def gen_if(self, n: N.If, target: str | None):
        self.emit(f"if {self.E(n.cond)}:")
        self.ind += 1
        self.gen_body(n.body, target)
        self.ind -= 1
        for cond, body in n.elifs:
            self.emit(f"elif {self.E(cond)}:")
            self.ind += 1
            self.gen_body(body, target)
            self.ind -= 1
        if n.orelse is not None:
            self.emit("else:")
            self.ind += 1
            self.gen_body(n.orelse, target)
            self.ind -= 1
        elif target is not None:
            self.emit("else:")
            self.emit(f"    {target} = na")

    def gen_switch(self, n: N.Switch, target: str | None):
        subject = None
        if n.subject is not None:
            subject = self.unique("_sw")
            self.emit(f"{subject} = {self.E(n.subject)}")
        keyword_ = "if"
        default = None
        for key, body in n.cases:
            if key is None:
                default = body
                continue
            cond = f"{subject} == {self.E(key, _PREC_ADD)}" if subject else self.E(key)
            self.emit(f"{keyword_} {cond}:")
            keyword_ = "elif"
            self.ind += 1
            self.gen_body(body, target)
            self.ind -= 1
        if default is not None:
            if keyword_ == "if":
                self.emit("if True:")
            else:
                self.emit("else:")
            self.ind += 1
            self.gen_body(default, target)
            self.ind -= 1
        elif target is not None and keyword_ == "elif":
            self.emit("else:")
            self.emit(f"    {target} = na")
        elif target is not None:
            self.emit(f"{target} = na")

    def gen_loop(self, n, target: str | None):
        if isinstance(n, N.For):
            info = self.declare(n.var, n)
            step = f", {self.E(n.step)}" if n.step is not None else ""
            self.emit(f"for {info.store} in loop_range({self.E(n.start)}, {self.E(n.end)}{step}):")
        else:
            guard = self.unique("_guard")
            self.emit(f"{guard} = LoopGuard({n.line})")
            self.emit(f"while {self.E(n.cond)}:")
            self.ind += 1
            self.emit(f"{guard}.tick()")
            self.ind -= 1
        self.ind += 1
        self.gen_body(n.body, target)
        self.ind -= 1

    # -- statement calls: plots, strategy, drawing ---------------------------------------------------------------

    def is_statement_call(self, c: N.Call) -> bool:
        f = c.func
        return (f in B.PLOT_FUNCS or f in ("alertcondition", "bgcolor", "barcolor", "fill", "indicator", "study",
                                            "strategy", "library") or f in SKIPPED_CALLS
                or f.startswith("strategy.") and f not in B.VARIABLES or f.startswith(DRAWING_NAMESPACES))

    def gen_expr_stmt(self, s: N.ExprStmt):
        e = s.expr
        if isinstance(e, N.Call):
            f = e.func
            if f in self.funcs:
                self.emit(self.E(e))
                return
            if f in ("indicator", "study", "strategy", "library"):
                self.error(f"{f}() can only appear once, as the first statement of the script", e)
            if f in B.PLOT_FUNCS:
                return self.gen_plot(e)
            if f == "alertcondition":
                return self.gen_alertcondition(e)
            if f in B.SKIPPED_DRAWING:
                self.warn(f"{f}() is not drawn by dashboard indicators and was skipped", e)
                return
            if f.startswith("strategy."):
                return self.gen_strategy_call(e)
            if f in SKIPPED_CALLS:
                if f == "alert":
                    self.warn("alert() has no alerts in the dashboard and was skipped", e)
                elif f != "max_bars_back":
                    self.warn(f"{f}() has no output here and was skipped", e)
                return
            if f.startswith(DRAWING_NAMESPACES):
                self.warn(f"{f}() draws objects on the chart, which dashboard indicators cannot do; skipped", e)
                return
        self.emit(self.E(e))

    def no_function_scope(self, c: N.Call):
        if self.scope.kind != "global":
            self.error(f"{c.func}() cannot be used inside a function", c)

    def title_of(self, got: dict, default: str) -> str:
        t = self.try_const(got.get("title"))
        return str(t) if isinstance(t, str) and t else default

    def color_args(self, got: dict, key: str = "color", default=None):
        """(code for the prologue or None, per-bar code or None)."""
        node = got.get(key)
        if node is None:
            return default, None
        if isinstance(node, N.Name) and node.id == "na":
            return default, None
        if self.is_static(node):
            return self.E(node), None
        return None, self.E(node)

    def gen_plot(self, c: N.Call):
        self.no_function_scope(c)
        f = c.func
        if f in ("plotcandle", "plotbar"):
            self.warn(f"{f}() is not supported and was skipped", c)
            return
        self.plot_count += 1
        if f == "plot":
            return self.gen_line_plot(c)
        if f == "hline":
            return self.gen_hline(c)
        if f in ("plotshape", "plotchar"):
            return self.gen_shape(c)
        if f == "plotarrow":
            return self.gen_arrow(c)

    def display_hidden(self, got: dict) -> bool:
        d = got.get("display")
        return d is not None and self.try_const(d) == "display.none"

    def gen_line_plot(self, c: N.Call):
        got = self.bind(c, ["series", "title", "color", "linewidth", "style", "trackprice", "histbase", "offset", "join",
                            "editable", "show_last", "display"])
        if "series" not in got:
            self.error("plot() needs a series to draw", c)
        value = self.E(got["series"])
        if self.display_hidden(got):
            self.warn("a plot with display=display.none is not drawn", c)
            self.emit(f"_ = {value}")
            return
        title = self.title_of(got, f"Plot {self.plot_count - 1}")
        sid = self.unique_id(title)
        style = self.try_const(got.get("style"), "plot.style_line")
        if got.get("style") is not None and self.try_const(got["style"], None) is None:
            self.warn("a plot style that depends on the data is drawn as a line", c)
        kind = B.PLOT_STYLES.get(style, "line") if isinstance(style, str) else "line"
        if isinstance(style, str) and style in B.PLOT_STYLE_NOTES:
            self.warn(B.PLOT_STYLE_NOTES[style], c)
        if got.get("histbase") is not None and self.try_const(got["histbase"], 0) != 0:
            self.warn("histbase is ignored; histograms are drawn from zero", c)
        static_color, bar_color = self.color_args(got)
        offset = self.try_const(got.get("offset"), 0)
        if not isinstance(offset, int):
            offset = 0
        var = f"p_{sid}"
        cargs = f"color={static_color}" if static_color else "color=None"
        width = self.try_const(got.get("linewidth"), 1)
        if not isinstance(width, int):
            width = 1
        if kind == "line":
            self.plot_decls.append(f"{var} = out.line({sid!r}, {ascii_repr(title)}, {cargs}, line_width={width}"
                                   + (f", offset={offset}" if offset else "") + ")")
        elif kind == "histogram":
            self.plot_decls.append(f"{var} = out.histogram({sid!r}, {ascii_repr(title)}, {cargs}"
                                   + (f", offset={offset}" if offset else "") + ")")
        else:
            self.plot_decls.append(f"{var} = out.markers({sid!r}, {ascii_repr(title)}, {cargs}"
                                   + (f", offset={offset}" if offset else "") + ")")
            tmp = self.unique("_v")
            self.emit(f"{tmp} = {value}")
            self.emit(f"{var}.add(not isna({tmp})" + (f", color={bar_color}" if bar_color else "") + ")")
            return
        self.emit(f"{var}.add({value}" + (f", {bar_color}" if bar_color else "") + ")")

    def gen_hline(self, c: N.Call):
        got = self.bind(c, ["price", "title", "color", "linestyle", "linewidth", "editable", "display"])
        if "price" not in got:
            self.error("hline() needs a price", c)
        title = self.title_of(got, f"Level {self.plot_count}")
        sid = self.unique_id("hline_" + title)
        style = B.HLINE_STYLES.get(self.try_const(got.get("linestyle")), "dotted") if got.get("linestyle") is not None else "dotted"
        static_color, _ = self.color_args(got, default=ascii_repr(B.DEFAULT_HLINE_COLOR))
        width = self.try_const(got.get("linewidth"), 1)
        var = f"p_{sid}"
        self.plot_decls.append(f"{var} = out.line({sid!r}, {ascii_repr(title)}, color={static_color}, "
                               f"line_width={width if isinstance(width, int) else 1}, line_style={style!r})")
        self.emit(f"{var}.add({self.E(got['price'])})")

    def gen_shape(self, c: N.Call):
        if c.func == "plotshape":
            names = ["series", "title", "style", "location", "color", "offset", "text", "textcolor", "editable", "size",
                     "show_last", "display"]
        else:
            names = ["series", "title", "char", "location", "color", "offset", "text", "textcolor", "editable", "size",
                     "show_last", "display"]
            self.warn("plotchar() is drawn as a circle marker (characters are not drawn)", c)
        got = self.bind(c, names)
        if "series" not in got:
            self.error(f"{c.func}() needs a series", c)
        cond = self.E(got["series"])
        if self.display_hidden(got):
            self.emit(f"_ = {cond}")
            return
        title = self.title_of(got, f"Marker {self.plot_count - 1}")
        sid = self.unique_id(title)
        shape = "circle"
        if c.func == "plotshape" and got.get("style") is not None:
            sname = self.try_const(got["style"])
            if sname is None:
                self.warn("a marker shape that depends on the data is drawn as a circle", c)
            else:
                shape = B.SHAPES.get(sname, "circle")
                if sname in ("shape.xcross", "shape.cross", "shape.diamond", "shape.flag", "shape.labelup", "shape.labeldown"):
                    self.warn(f"{sname} is drawn as {shape}", c)
        position = "aboveBar"
        if got.get("location") is not None:
            loc = self.try_const(got["location"])
            if loc is None:
                self.warn("a marker location that depends on the data is drawn above the bar", c)
            else:
                position = B.LOCATIONS.get(loc, "aboveBar")
                if loc == "location.absolute":
                    self.warn("location.absolute is drawn above the bar (markers cannot sit at a price)", c)
        elif c.func == "plotshape":
            position = "aboveBar"
        static_color, bar_color = self.color_args(got, default=ascii_repr(B.DEFAULT_PLOT_COLOR))
        text_static, text_bar = None, None
        if got.get("text") is not None:
            if self.is_static(got["text"]):
                text_static = self.E(got["text"])
            else:
                text_bar = self.E(got["text"])
        size = B.SIZES.get(self.try_const(got.get("size")))
        offset = self.try_const(got.get("offset"), 0)
        offset = offset if isinstance(offset, int) else 0
        var = f"p_{sid}"
        decl = f"{var} = out.markers({sid!r}, {ascii_repr(title)}, color={static_color}, shape={shape!r}, position={position!r}"
        if text_static:
            decl += f", text={text_static}"
        if size is not None:
            decl += f", size={size}"
        if offset:
            decl += f", offset={offset}"
        self.plot_decls.append(decl + ")")
        extra = ""
        if bar_color:
            extra += f", color={bar_color}"
        if text_bar:
            extra += f", text={text_bar}"
        self.emit(f"{var}.add({cond}{extra})")

    def gen_arrow(self, c: N.Call):
        got = self.bind(c, ["series", "title", "colorup", "colordown", "offset", "minheight", "maxheight", "editable",
                            "show_last", "display"])
        if "series" not in got:
            self.error("plotarrow() needs a series", c)
        value = self.E(got["series"])
        title = self.title_of(got, f"Arrow {self.plot_count - 1}")
        sid = self.unique_id(title)
        up = self.E(got["colorup"]) if got.get("colorup") is not None else ascii_repr(B.COLORS["teal"])
        down = self.E(got["colordown"]) if got.get("colordown") is not None else ascii_repr(B.COLORS["red"])
        var = f"p_{sid}"
        self.plot_decls.append(f"{var} = out.markers({sid!r}, {ascii_repr(title)}, color={up})")
        self.emit(f"{var}.add_arrow({value}, {up}, {down})")

    def gen_alertcondition(self, c: N.Call):
        self.no_function_scope(c)
        got = self.bind(c, ["condition", "title", "message"])
        if "condition" not in got:
            self.error("alertcondition() needs a condition", c)
        self.plot_count += 1
        title = self.title_of(got, f"Alert {self.plot_count - 1}")
        self.warn("alertcondition() cannot send alerts here; the condition is drawn as markers instead", c)
        sid = self.unique_id("alert_" + title)
        var = f"p_{sid}"
        self.plot_decls.append(f"{var} = out.markers({sid!r}, {ascii_repr(title)}, color={ascii_repr(B.ALERT_COLOR)})")
        self.emit(f"{var}.add({self.E(got['condition'])})")

    def gen_strategy_call(self, c: N.Call):
        f = c.func
        if self.scope.kind != "global":
            self.error(f"{f}() cannot be used inside a function", c)
        if f not in B.STRATEGY_ORDER_CALLS:
            self.warn(f"{f}() is not converted (order logic is not converted) and was skipped", c)
            return
        got = self.bind(c, B.STRATEGY_ORDER_CALLS[f])
        kind = f.split(".", 1)[1]
        idnode = got.get("id")
        ident = self.try_const(idnode) if idnode is not None else None
        if f == "strategy.close_all":
            ident = "close all"
        elif not isinstance(ident, str):
            ident = "strategy order"
        suffix = {"entry": "", "order": "", "exit": " (exit)", "close": " (close)", "close_all": ""}[kind]
        key = (kind, ident)
        var = self.strategy_series.get(key)
        if var is None:
            sid = self.unique_id(ident)
            var = f"p_{sid}"
            self.strategy_series[key] = var
            self.plot_decls.append(f"{var} = out.markers({sid!r}, {ascii_repr(ident + suffix)}, "
                                   f"color={ascii_repr(B.STRATEGY_COLOR)}, shape='circle', position='aboveBar')")
        when = self.E(got["when"]) if got.get("when") is not None else "True"
        self.emit(f"{var}.add({when})")

    # -- functions -----------------------------------------------------------------------------------------------

    def gen_function(self, fd: N.FuncDef):
        scope = Scope("func", self.global_scope)
        outer = (self.scope, self.lines, self.ind)
        self.scope, self.lines, self.ind = scope, [], 2
        params = []
        for p in fd.params:
            info = self.declare(p.name, p)
            params.append(info.store)
            if info.hist:
                scope.prologue.append(f"{info.hist}.push({info.store})")
        body = fd.body
        last = body[-1] if body else None
        if last is None:
            self.error("a function needs a body", fd)
        for stmt in body[:-1]:
            self.gen_stmt(stmt)
        if isinstance(last, N.ExprStmt) and not (isinstance(last.expr, N.Call) and self.is_statement_call(last.expr)):
            self.emit(f"return {self.E(last.expr)}")
        elif isinstance(last, (N.If, N.Switch, N.For, N.While)):
            self.emit("_ret = na")
            self.gen_value_stmt(last, "_ret")
            self.emit("return _ret")
        elif isinstance(last, (N.VarDecl, N.Assign)):
            self.gen_stmt(last)
            self.emit(f"return {self.lookup(last.name).code}")
        elif isinstance(last, N.TupleDecl):
            self.gen_stmt(last)
            self.emit(f"return ({', '.join(self.lookup(n).code for n in last.names)})")
        else:
            self.gen_stmt(last)
            self.emit("return na")
        header = [f"    def {self.func_py(fd.name)}({', '.join(['st'] + params)}):"]
        pro = (["        _v = st.vars"] if scope.uses_vars else []) + ["        " + line for line in scope.prologue]
        text = header + pro + self.lines
        self.scope, self.lines, self.ind = outer
        self.func_lines.extend(text)
        self.func_lines.append("")

    # -- the whole script ---------------------------------------------------------------------------------------

    def run(self) -> tuple[str, str, bool]:
        script = self.script
        body = list(script.body)
        decl_call = None
        rest = []
        for s in body:
            if decl_call is None and isinstance(s, N.ExprStmt) and isinstance(s.expr, N.Call) \
                    and s.expr.func in ("indicator", "study", "strategy"):
                decl_call = s.expr
                continue
            rest.append(s)
        self.read_declaration(decl_call)
        for s in rest:
            if isinstance(s, N.FuncDef):
                if s.name in self.funcs:
                    self.error(f"the function '{s.name}' is defined twice", s)
                self.funcs[s.name] = s
        for node in _walk(rest):
            if isinstance(node, N.Index) and isinstance(node.target, N.Name):
                self.indexed.add(node.target.id)
        for fd in self.funcs.values():
            self.gen_function(fd)
        for s in rest:
            if isinstance(s, N.FuncDef):
                continue
            self.gen_stmt(s)
        if not self.plot_decls:
            self.warn("the script draws nothing (no plot, plotshape, hline or similar call)")
        return self.assemble()

    def read_declaration(self, c: N.Call | None):
        if c is None:
            self.warn("no indicator() or strategy() declaration found; treating the script as an indicator in its own pane")
            self.title = "Pine script"
            return
        names = ["title", "shorttitle", "overlay"] if c.func != "strategy" else ["title", "shorttitle", "overlay"]
        got = self.bind(c, names)
        title = self.try_const(got.get("title"))
        self.title = str(title) if isinstance(title, str) and title else "Pine script"
        ov = self.try_const(got.get("overlay"), False)
        self.overlay = bool(ov)
        if c.func == "strategy":
            self.is_strategy = True
            self.warn("this is a strategy() script: order logic (entries, exits, sizing, risk settings) is not converted; "
                      "only the conditions behind strategy.entry/exit/close calls are drawn as markers")

    def assemble(self) -> tuple[str, str, bool]:
        title = self.title
        name = self.name_hint or slug(title, "pine")
        lines = []
        header_title = " ".join(title.encode("ascii", "replace").decode("ascii").split())
        lines.append(f"# generated by tools.pine2py from {header_title} - do not edit by hand")
        if self.is_strategy:
            lines.append("# NOTE: this was a strategy() script. Order logic (entries, exits, sizing, risk settings) is NOT")
            lines.append("# converted; only the conditions behind strategy.entry/exit/close calls are drawn as markers.")
        doc_title = re.sub(r'["\\]', "", header_title) or "Converted indicator"
        lines.append(f'"""{doc_title} (converted from Pine Script by tools.pine2py).')
        lines.append("")
        lines.append("A line on the chart drawn with the settings below. Nothing here is a signal.")
        lines.append('"""')
        lines.append("")
        body = self.build_body()
        skip = {"annotations", "math", "statistics", "datetime", "timezone"}
        imports = sorted(n for n in dir(rt) if not n.startswith("_") and n not in skip
                         and re.search(rf"(?<![\w.]){re.escape(n)}(?!\w)", body))
        if imports:
            lines.append("from tools.pine2py.runtime import (")
            lines.extend(f"    {n}," for n in imports)
            lines.append(")")
            lines.append("")
        lines.append(f"NAME = {self.name_literal(name)}")
        lines.append(f"TITLE = {ascii_repr(title)}")
        lines.append(f"DESCRIPTION = {ascii_repr('Converted from Pine Script by tools.pine2py: ' + title)}")
        lines.append(f"OVERLAY = {self.overlay}")
        lines.append("PARAMS = [")
        for p in self.params:
            lines.append("    {" + ", ".join(f"{k!r}: {self.param_value(v)}" for k, v in p.items()) + "},")
        lines.append("]")
        lines.append("")
        lines.append("")
        lines.append(body)
        return "\n".join(lines) + "\n", name, self.overlay

    def name_literal(self, name):
        return ascii_repr(name)

    def param_value(self, v):
        if isinstance(v, str):
            return ascii_repr(v)
        if isinstance(v, list):
            return "[" + ", ".join(self.param_value(i) for i in v) + "]"
        return repr(v)

    def build_body(self) -> str:
        out = ["def compute(candles, params):",
               "    ctx = Context(candles, params)",
               "    out = Output(ctx)",
               "    root = ctx.state()"]
        out.extend("    " + line for line in self.param_lines)
        out.extend("    " + line for line in self.plot_decls)
        out.extend("    " + line for line in self.global_scope.prologue)
        if self.func_lines:
            out.append("")
            out.extend(self.func_lines)
        out.append("    for bar in ctx.bars():")
        out.extend(self.lines if self.lines else ["        pass"])
        out.append("    return out.result()")
        return "\n".join(out)


def N_ternary(gen: Generator, args: list) -> str:
    c, a, b = args
    return f"{gen.E(a, _PREC_OR)} if {gen.E(c, _PREC_OR)} else {gen.E(b, _PREC_TERNARY)}"


def generate(script: N.Script, version: int | None = None, name: str | None = None):
    gen = Generator(script, version, name)
    code, final_name, overlay = gen.run()
    return code, gen.warnings, final_name, gen.title, overlay
