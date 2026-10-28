"""Pine Script lexer: source text -> tokens, with INDENT / DEDENT for blocks.

Pine's layout rules, as implemented here:

- A block (the body of ``if``, ``for``, ``while``, ``switch``, a function, a switch case) is
  indented by exactly one level: 4 spaces or one tab more than the line that opens it.
- A line indented by a number of spaces that is *not* a multiple of 4 continues the previous
  line ("line wrapping"). So does any line while a ``(`` or ``[`` is still open, and (a lenient
  extra) a line after one that ends with a binary operator or a comma, or a deeper-indented
  line that starts with one.
- ``//`` starts a comment. ``//@version=N`` is the version pragma.

Tokens: NAME (identifiers; dotted names such as ``ta.sma`` are kept whole), NUMBER, STRING,
COLOR (``#rrggbb`` / ``#rrggbbaa``), OP, KW (keywords), NEWLINE, INDENT, DEDENT, EOF.
Columns are 1-based and count a tab as 4 spaces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import PineSyntaxError

KEYWORDS = frozenset({
    "and", "or", "not", "if", "else", "for", "to", "by", "while", "switch", "var", "varip",
    "true", "false", "break", "continue", "import", "export", "method", "type", "in",
})

_TOKEN = re.compile(r"""
    (?P<ws>[ ]+)
  | (?P<comment>//.*)
  | (?P<number>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
  | (?P<color>\#(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{6})(?![0-9A-Za-z_]))
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
  | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<op>:=|=>|==|!=|<=|>=|\+=|-=|\*=|/=|%=|[-+*/%<>=?:,()\[\]])
""", re.X)

_VERSION = re.compile(r"^//\s*@version\s*=\s*(\d+)")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}

# A line that ends with one of these continues on the next line (lenient wrapping).
_TRAILING_CONTINUES = frozenset({"+", "-", "*", "/", "%", "==", "!=", "<", ">", "<=", ">=", "?", ":", ",",
                                 "=", ":=", "+=", "-=", "*=", "/=", "%="})
# A deeper-indented line that starts with one of these continues the previous line.
_LEADING_CONTINUES = frozenset({"*", "/", "%", "==", "!=", "<", ">", "<=", ">=", "?", ":", ","})


@dataclass
class Token:
    type: str          # NAME NUMBER STRING COLOR OP KW NEWLINE INDENT DEDENT EOF
    value: object
    line: int
    col: int

    def is_op(self, *values: str) -> bool:
        return self.type == "OP" and self.value in values

    def is_kw(self, *values: str) -> bool:
        return self.type == "KW" and self.value in values

    def __repr__(self) -> str:  # compact, for test failures
        return f"{self.type}({self.value!r})@{self.line}:{self.col}"


@dataclass
class Lexed:
    tokens: list
    version: int | None
    version_line: int
    lines: list            # physical source lines (tabs expanded)


def _unescape(raw: str) -> str:
    body = raw[1:-1]
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _scan_line(text: str, lineno: int, depth_stack: list, version_box: list) -> list:
    """Tokens of one physical line (no NEWLINE). Tracks open brackets in ``depth_stack``."""
    tokens = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        col = pos + 1
        if m is None:
            ch = text[pos]
            if ch in "\"'":
                raise PineSyntaxError("this string is not closed on the same line", lineno, col)
            if ch == ".":
                raise PineSyntaxError("'.' after an expression (a method call or a field) is not supported", lineno, col)
            if ch == "#":
                raise PineSyntaxError("a color literal must be #rrggbb or #rrggbbaa", lineno, col)
            raise PineSyntaxError(f"unexpected character {ch!r}", lineno, col)
        kind = m.lastgroup
        value = m.group(kind)
        pos = m.end()
        if kind == "ws":
            continue
        if kind == "comment":
            vm = _VERSION.match(value)
            if vm and version_box[0] is None:
                version_box[0] = int(vm.group(1))
                version_box[1] = lineno
            break
        if kind == "number":
            is_float = any(c in value for c in ".eE")
            tokens.append(Token("NUMBER", float(value) if is_float else int(value), lineno, col))
        elif kind == "color":
            tokens.append(Token("COLOR", value.lower(), lineno, col))
        elif kind == "name":
            tokens.append(Token("KW" if value in KEYWORDS else "NAME", value, lineno, col))
        elif kind == "string":
            tokens.append(Token("STRING", _unescape(value), lineno, col))
        else:
            tok = Token("OP", value, lineno, col)
            if value in "([":
                depth_stack.append(tok)
            elif value in ")]":
                if not depth_stack:
                    raise PineSyntaxError(f"'{value}' without a matching opening bracket", lineno, col)
                opener = depth_stack.pop()
                if (opener.value, value) not in (("(", ")"), ("[", "]")):
                    raise PineSyntaxError(
                        f"'{value}' does not match the '{opener.value}' opened at line {opener.line}, col {opener.col}",
                        lineno, col)
            tokens.append(tok)
    return tokens


def tokenize(source: str) -> Lexed:
    """Split Pine source into tokens. Raises PineSyntaxError with the line/col of the first problem."""
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    if source.startswith("﻿"):
        source = source[1:]
    lines = [line.replace("\t", "    ") for line in source.split("\n")]
    version_box: list = [None, 0]
    depth_stack: list = []
    out: list = []
    levels = [0]
    logical: list | None = None     # tokens of the logical line being built
    logical_indent = 0

    def close_logical_line():
        if logical:
            last = logical[-1]
            out.append(Token("NEWLINE", None, last.line, last.col + 1))

    for index, text in enumerate(lines):
        lineno = index + 1
        stripped = text.lstrip(" ")
        indent = len(text) - len(stripped)
        open_before = bool(depth_stack)
        tokens = _scan_line(text, lineno, depth_stack, version_box)
        if not tokens:
            continue    # blank or comment-only lines never change the indentation
        first = tokens[0]
        continuation = False
        if logical is not None:
            last = logical[-1]
            if open_before:
                continuation = True
            elif (last.type == "OP" and last.value in _TRAILING_CONTINUES) or last.is_kw("and", "or", "not"):
                continuation = True
            elif indent % 4 != 0:
                continuation = True
            elif indent > logical_indent and ((first.type == "OP" and first.value in _LEADING_CONTINUES)
                                              or first.is_kw("and", "or")):
                continuation = True
        if continuation:
            logical.extend(tokens)
            out.extend(tokens)
            continue
        if indent % 4 != 0:
            raise PineSyntaxError(
                f"this line is indented by {indent} spaces; Pine blocks use 4 spaces (or one tab) per level "
                "and other indents continue the previous line", lineno, 1)
        close_logical_line()
        level = indent // 4
        if level > levels[-1]:
            if level != levels[-1] + 1:
                raise PineSyntaxError("this line is indented more than one level deeper than the line before it",
                                      lineno, indent + 1)
            levels.append(level)
            out.append(Token("INDENT", level, lineno, indent + 1))
        else:
            while level < levels[-1]:
                levels.pop()
                out.append(Token("DEDENT", level, lineno, 1))
        out.extend(tokens)
        logical = tokens[:]
        logical_indent = indent

    if depth_stack:
        opener = depth_stack[-1]
        raise PineSyntaxError(f"'{opener.value}' is never closed", opener.line, opener.col)
    close_logical_line()
    end_line = len(lines) + 1
    while len(levels) > 1:
        levels.pop()
        out.append(Token("DEDENT", levels[-1], end_line, 1))
    out.append(Token("EOF", None, end_line, 1))
    return Lexed(out, version_box[0], version_box[1], lines)
