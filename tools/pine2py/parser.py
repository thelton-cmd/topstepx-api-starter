"""Pine Script parser: tokens -> AST (see nodes.py).

Grammar, highest precedence first (Pine v5):

    postfix   x[n] (history), f(args)
    unary     - + not
    mul       * / %
    add       + -
    compare   < > <= >=
    equality  == !=
    and, or
    ternary   cond ? a : b          (right associative)

Statements: declarations (``x = e``, ``var x = e``, ``varip``, ``float x = e``), tuple
declarations ``[a, b] = f()``, reassignment ``x := e`` / ``x += e``, ``if / else if / else``,
``for i = a to b [by s]``, ``for x in arr``, ``while``, ``switch``, functions ``f(a, b) => e``
(single line or an indented block), ``break``, ``continue`` and expression statements such as
``plot(...)``. ``if``, ``switch``, ``for`` and ``while`` can also be the value of a declaration
or reassignment. ``import``, ``type``, ``method`` and field assignments are read and returned
as ``Skipped`` nodes so the converter can warn about them and keep going.
"""

from __future__ import annotations

from . import nodes as N
from .errors import PineSyntaxError
from .lexer import Lexed, Token, tokenize

QUALIFIERS = frozenset({"series", "simple", "const", "input"})
ASSIGN_OPS = frozenset({":=", "+=", "-=", "*=", "/=", "%="})
GENERIC_CALL_PREFIXES = ("array.", "matrix.", "map.", "request.")


def _describe(tok: Token) -> str:
    if tok.type == "NEWLINE":
        return "end of line"
    if tok.type == "EOF":
        return "end of script"
    if tok.type == "INDENT":
        return "indentation"
    if tok.type == "DEDENT":
        return "end of block"
    if tok.type == "STRING":
        return f"string {tok.value!r}"
    return f"'{tok.value}'"


class Parser:
    def __init__(self, lexed: Lexed):
        self.toks = lexed.tokens
        self.i = 0

    # -- token helpers ---------------------------------------------------------------------

    def peek(self, k: int = 0) -> Token:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self) -> Token:
        tok = self.toks[self.i]
        if tok.type != "EOF":
            self.i += 1
        return tok

    def error(self, message: str, tok: Token | None = None):
        tok = tok or self.peek()
        raise PineSyntaxError(message, tok.line, tok.col)

    def expect_op(self, value: str, context: str = "") -> Token:
        tok = self.peek()
        if not tok.is_op(value):
            where = f" {context}" if context else ""
            self.error(f"expected '{value}'{where}, found {_describe(tok)}", tok)
        return self.next()

    def expect_name(self, context: str) -> Token:
        tok = self.peek()
        if tok.type != "NAME":
            self.error(f"expected a name {context}, found {_describe(tok)}", tok)
        return self.next()

    def end_stmt(self) -> int:
        tok = self.peek()
        if tok.type == "NEWLINE":
            self.next()
            return tok.line
        if tok.type in ("EOF", "DEDENT"):
            return self.toks[self.i - 1].line if self.i else tok.line
        if tok.type == "INDENT":
            self.error("unexpected indentation: only if/for/while/switch/functions open an indented block", tok)
        self.error(f"unexpected {_describe(tok)}", tok)

    def finish(self, value) -> int:
        """End of a declaration/assignment: structures end with their block, others with the line."""
        if isinstance(value, N.STRUCTURES):
            return self.toks[self.i - 1].line
        return self.end_stmt()

    # -- script and blocks ---------------------------------------------------------------------

    def parse(self) -> N.Script:
        body = []
        while self.peek().type != "EOF":
            tok = self.peek()
            if tok.type in ("NEWLINE", "DEDENT"):
                self.next()
                continue
            if tok.type == "INDENT":
                self.error("unexpected indentation: nothing above this line opens a block", tok)
            body.append(self.statement())
        return N.Script(1, 1, body)

    def block(self, what: str) -> list:
        tok = self.peek()
        if tok.type != "NEWLINE":
            self.error(f"unexpected {_describe(tok)}; the {what} body starts on the next line", tok)
        self.next()
        tok = self.peek()
        if tok.type != "INDENT":
            self.error(f"expected an indented {what} body (4 spaces or one tab deeper) on this line", tok)
        self.next()
        body = []
        while True:
            tok = self.peek()
            if tok.type == "DEDENT":
                self.next()
                break
            if tok.type == "EOF":
                break
            if tok.type == "NEWLINE":
                self.next()
                continue
            if tok.type == "INDENT":
                self.error("unexpected indentation", tok)
            body.append(self.statement())
        return body

    def skip_rest(self, what: str, tok: Token) -> N.Skipped:
        """Skip to the end of the logical line, plus an indented block that follows it."""
        while self.peek().type not in ("NEWLINE", "EOF"):
            self.next()
        end = self.peek().line
        if self.peek().type == "NEWLINE":
            self.next()
        if self.peek().type == "INDENT":
            depth = 0
            while self.peek().type != "EOF":
                t = self.next()
                if t.type == "INDENT":
                    depth += 1
                elif t.type == "DEDENT":
                    depth -= 1
                    if depth == 0:
                        break
                end = t.line
        return N.Skipped(tok.line, tok.col, what, end)

    # -- statements -----------------------------------------------------------------------------

    def statement(self):
        tok = self.peek()
        if tok.type == "KW":
            kw = tok.value
            if kw in ("var", "varip"):
                self.next()
                type_name = self.type_prefix()
                return self.declaration(kw, type_name)
            if kw == "if":
                return self.if_struct()
            if kw == "for":
                return self.for_struct()
            if kw == "while":
                return self.while_struct()
            if kw == "switch":
                return self.switch_struct()
            if kw == "break":
                self.next()
                self.end_stmt()
                return N.Break(tok.line, tok.col)
            if kw == "continue":
                self.next()
                self.end_stmt()
                return N.Continue(tok.line, tok.col)
            if kw == "import":
                return self.skip_rest("import (libraries)", tok)
            if kw == "type":
                return self.skip_rest("type (user-defined types)", tok)
            if kw == "method":
                return self.skip_rest("method (user-defined methods)", tok)
            if kw == "export":
                self.next()
                return self.statement()
            if kw == "else":
                self.error("'else' without a matching 'if' above it at the same indentation", tok)
        if tok.type == "NAME":
            type_name = self.type_prefix()
            if type_name is not None:
                return self.declaration(None, type_name)
            nxt = self.peek(1)
            if nxt.is_op("="):
                return self.declaration(None, None)
            if nxt.type == "OP" and nxt.value in ASSIGN_OPS:
                return self.assignment()
            if nxt.is_op("(") and self.is_func_def():
                return self.func_def()
        if tok.is_op("[") and self.is_tuple_decl():
            return self.tuple_decl()
        expr = self.expression()
        end = self.end_stmt()
        return N.ExprStmt(tok.line, tok.col, expr, end)

    def type_prefix(self) -> str | None:
        """``[qualifier] type[<args>]`` followed by ``name =``: consume the type and return it."""
        j = 0
        parts = []
        tok = self.peek(j)
        if tok.type == "NAME" and tok.value in QUALIFIERS and self.peek(j + 1).type == "NAME":
            parts.append(tok.value)
            j += 1
        tok = self.peek(j)
        if tok.type != "NAME":
            return None
        parts.append(tok.value)
        j += 1
        if self.peek(j).is_op("<"):
            depth = 0
            text = ""
            while True:
                t = self.peek(j)
                if t.type in ("NEWLINE", "EOF"):
                    return None
                if t.is_op("<"):
                    depth += 1
                elif t.is_op(">"):
                    depth -= 1
                elif not (t.type == "NAME" or t.is_op(",")):
                    return None
                text += str(t.value)
                j += 1
                if depth == 0:
                    break
            parts[-1] += text
        if self.peek(j).type == "NAME" and self.peek(j + 1).is_op("="):
            for _ in range(j):
                self.next()
            return " ".join(parts)
        return None

    def value(self):
        """The right-hand side of a declaration: an expression or an if/switch/for/while structure."""
        tok = self.peek()
        if tok.is_kw("if"):
            return self.if_struct()
        if tok.is_kw("switch"):
            return self.switch_struct()
        if tok.is_kw("for"):
            return self.for_struct()
        if tok.is_kw("while"):
            return self.while_struct()
        return self.expression()

    def declaration(self, mode: str | None, type_name: str | None):
        name = self.expect_name("for the new variable")
        if "." in name.value:
            self.error(f"'{name.value}' cannot be declared: a variable name has no dots", name)
        self.expect_op("=", "after the variable name")
        value = self.value()
        end = self.finish(value)
        return N.VarDecl(name.line, name.col, name.value, value, mode, type_name, end)

    def assignment(self):
        name = self.next()
        op = self.next().value
        if "." in name.value:
            return self._field_assign(name)
        value = self.value()
        end = self.finish(value)
        return N.Assign(name.line, name.col, name.value, op, value, end)

    def _field_assign(self, name: Token):
        value = self.value()
        end = self.finish(value)
        return N.Skipped(name.line, name.col, "assigning to a field (user-defined types)", end)

    def is_tuple_decl(self) -> bool:
        j = 1
        while True:
            tok = self.peek(j)
            if tok.type != "NAME":
                return False
            j += 1
            tok = self.peek(j)
            if tok.is_op(","):
                j += 1
                continue
            if tok.is_op("]"):
                return self.peek(j + 1).is_op("=")
            return False

    def tuple_decl(self):
        start = self.next()
        names = []
        while True:
            names.append(self.expect_name("in the tuple").value)
            if self.peek().is_op(","):
                self.next()
                continue
            break
        self.expect_op("]", "to close the tuple")
        self.expect_op("=", "after the tuple")
        value = self.value()
        end = self.finish(value)
        return N.TupleDecl(start.line, start.col, names, value, end)

    def is_func_def(self) -> bool:
        j = 1
        depth = 0
        while True:
            tok = self.peek(j)
            if tok.type in ("EOF", "NEWLINE"):
                return False
            if tok.is_op("(", "["):
                depth += 1
            elif tok.is_op(")", "]"):
                depth -= 1
                if depth == 0:
                    return self.peek(j + 1).is_op("=>")
            j += 1

    def func_def(self):
        name = self.next()
        if "." in name.value:
            self.error("a function name has no dots (methods are not supported)", name)
        self.expect_op("(")
        params = []
        while not self.peek().is_op(")"):
            words = []
            first = self.peek()
            while self.peek().type == "NAME":
                word = self.next().value
                if self.peek().is_op("<"):          # array<float> src
                    while not self.peek().is_op(">"):
                        if self.peek().type in ("NEWLINE", "EOF"):
                            self.error("unclosed '<' in a parameter type")
                        word += str(self.next().value)
                    word += str(self.next().value)
                words.append(word)
            if not words:
                self.error(f"expected a parameter name, found {_describe(self.peek())}")
            default = None
            if self.peek().is_op("="):
                self.next()
                default = self.expression()
            params.append(N.Param(first.line, first.col, words[-1], default, " ".join(words[:-1]) or None))
            if self.peek().is_op(","):
                self.next()
                continue
            if not self.peek().is_op(")"):
                self.error(f"expected ',' or ')' in the parameter list, found {_describe(self.peek())}")
        self.next()
        self.expect_op("=>")
        if self.peek().type == "NEWLINE":
            body = self.block("function")
            end = self.toks[self.i - 1].line
        else:
            tok = self.peek()
            expr = self.value()
            end = self.finish(expr)
            body = [expr if isinstance(expr, N.STRUCTURES) else N.ExprStmt(tok.line, tok.col, expr, end)]
        return N.FuncDef(name.line, name.col, name.value, params, body, end)

    def if_struct(self):
        tok = self.next()
        cond = self.expression()
        body = self.block("if")
        elifs = []
        orelse = None
        while self.peek().is_kw("else"):
            self.next()
            if self.peek().is_kw("if"):
                self.next()
                c = self.expression()
                elifs.append((c, self.block("else if")))
                continue
            orelse = self.block("else")
            break
        return N.If(tok.line, tok.col, cond, body, elifs, orelse)

    def for_struct(self):
        tok = self.next()
        if self.peek().is_op("[") or (self.peek().type == "NAME" and self.peek(1).is_kw("in")):
            names = []
            if self.peek().is_op("["):
                self.next()
                while not self.peek().is_op("]"):
                    names.append(self.expect_name("in the loop variables").value)
                    if self.peek().is_op(","):
                        self.next()
                self.next()
            else:
                names.append(self.next().value)
            if not self.peek().is_kw("in"):
                self.error("expected 'in' in the for...in loop")
            self.next()
            iterable = self.expression()
            return N.ForIn(tok.line, tok.col, names, iterable, self.block("for"))
        var = self.expect_name("for the loop counter")
        self.expect_op("=", "after the loop counter")
        start = self.expression()
        if not self.peek().is_kw("to"):
            self.error(f"expected 'to' in the for loop, found {_describe(self.peek())}")
        self.next()
        end = self.expression()
        step = None
        if self.peek().is_kw("by"):
            self.next()
            step = self.expression()
        return N.For(tok.line, tok.col, var.value, start, end, step, self.block("for"))

    def while_struct(self):
        tok = self.next()
        cond = self.expression()
        return N.While(tok.line, tok.col, cond, self.block("while"))

    def switch_struct(self):
        tok = self.next()
        subject = None if self.peek().type == "NEWLINE" else self.expression()
        if self.peek().type != "NEWLINE":
            self.error(f"unexpected {_describe(self.peek())} after the switch")
        self.next()
        if self.peek().type != "INDENT":
            self.error("expected the indented switch cases on this line")
        self.next()
        cases = []
        while True:
            t = self.peek()
            if t.type == "DEDENT":
                self.next()
                break
            if t.type == "EOF":
                break
            if t.type == "NEWLINE":
                self.next()
                continue
            if t.is_op("=>"):
                self.next()
                key = None
            else:
                key = self.expression()
                self.expect_op("=>", "after the switch case")
            if self.peek().type == "NEWLINE":
                body = self.block("switch case")
            else:
                body = [self.statement()]
            cases.append((key, body))
        return N.Switch(tok.line, tok.col, subject, cases)

    # -- expressions -----------------------------------------------------------------------------

    def expression(self):
        return self.ternary()

    def ternary(self):
        cond = self.or_expr()
        if self.peek().is_op("?"):
            self.next()
            then = self.ternary()
            self.expect_op(":", "in the ?: expression")
            other = self.ternary()
            return N.Ternary(cond.line, cond.col, cond, then, other)
        return cond

    def or_expr(self):
        left = self.and_expr()
        while self.peek().is_kw("or"):
            self.next()
            left = N.Binary(left.line, left.col, "or", left, self.and_expr())
        return left

    def and_expr(self):
        left = self.equality()
        while self.peek().is_kw("and"):
            self.next()
            left = N.Binary(left.line, left.col, "and", left, self.equality())
        return left

    def equality(self):
        left = self.comparison()
        while self.peek().is_op("==", "!="):
            op = self.next().value
            left = N.Binary(left.line, left.col, op, left, self.comparison())
        return left

    def comparison(self):
        left = self.additive()
        while self.peek().is_op("<", ">", "<=", ">="):
            op = self.next().value
            left = N.Binary(left.line, left.col, op, left, self.additive())
        return left

    def additive(self):
        left = self.multiplicative()
        while self.peek().is_op("+", "-"):
            op = self.next().value
            left = N.Binary(left.line, left.col, op, left, self.multiplicative())
        return left

    def multiplicative(self):
        left = self.unary()
        while self.peek().is_op("*", "/", "%"):
            op = self.next().value
            left = N.Binary(left.line, left.col, op, left, self.unary())
        return left

    def unary(self):
        tok = self.peek()
        if tok.is_op("-", "+") or tok.is_kw("not"):
            self.next()
            return N.Unary(tok.line, tok.col, tok.value, self.unary())
        return self.postfix()

    def postfix(self):
        node = self.primary()
        while self.peek().is_op("["):
            self.next()
            offset = self.expression()
            self.expect_op("]", "to close the history reference")
            node = N.Index(node.line, node.col, node, offset)
        return node

    def primary(self):
        tok = self.peek()
        if tok.type == "NUMBER":
            self.next()
            return N.Num(tok.line, tok.col, tok.value, isinstance(tok.value, int))
        if tok.type == "STRING":
            self.next()
            return N.Str(tok.line, tok.col, tok.value)
        if tok.type == "COLOR":
            self.next()
            return N.ColorLit(tok.line, tok.col, tok.value)
        if tok.is_kw("true", "false"):
            self.next()
            return N.Bool(tok.line, tok.col, tok.value == "true")
        if tok.type == "NAME":
            self.next()
            if self.peek().is_op("("):
                return self.call(tok, None)
            if self.peek().is_op("<") and tok.value.startswith(GENERIC_CALL_PREFIXES):
                generic = self.try_generic()
                if generic is not None:
                    return self.call(tok, generic)
            return N.Name(tok.line, tok.col, tok.value)
        if tok.is_op("("):
            self.next()
            inner = self.expression()
            self.expect_op(")", "to close the parenthesis")
            return inner
        if tok.is_op("["):
            self.next()
            items = []
            while not self.peek().is_op("]"):
                items.append(self.expression())
                if self.peek().is_op(","):
                    self.next()
                    continue
                if not self.peek().is_op("]"):
                    self.error(f"expected ',' or ']' in the tuple, found {_describe(self.peek())}")
            self.next()
            return N.TupleExpr(tok.line, tok.col, items)
        if tok.is_kw("if", "switch", "for", "while"):
            self.error(f"'{tok.value}' can only start a statement or the value of a declaration (x = {tok.value} ...)", tok)
        self.error(f"expected a value, found {_describe(tok)}", tok)

    def try_generic(self) -> str | None:
        start = self.i
        text = ""
        depth = 0
        while True:
            t = self.peek()
            if t.is_op("<"):
                depth += 1
            elif t.is_op(">"):
                depth -= 1
            elif not (t.type == "NAME" or t.is_op(",")):
                self.i = start
                return None
            text += str(t.value)
            self.next()
            if depth == 0:
                break
        if not self.peek().is_op("("):
            self.i = start
            return None
        return text

    def call(self, name: Token, generic: str | None):
        self.expect_op("(")
        args = []
        kwargs = {}
        while not self.peek().is_op(")"):
            tok = self.peek()
            if tok.type in ("NAME", "KW") and self.peek(1).is_op("=") and "." not in str(tok.value):
                self.next()
                self.next()
                if tok.value in kwargs:
                    self.error(f"argument '{tok.value}' is given twice", tok)
                kwargs[tok.value] = self.expression()
            else:
                if kwargs:
                    self.error("a positional argument cannot follow a named argument", tok)
                args.append(self.expression())
            if self.peek().is_op(","):
                self.next()
                continue
            if not self.peek().is_op(")"):
                self.error(f"expected ',' or ')' in the call to {name.value}(), found {_describe(self.peek())}")
        self.next()
        return N.Call(name.line, name.col, name.value, args, kwargs, generic)


def parse(source: str) -> tuple[N.Script, Lexed]:
    """Tokenize and parse Pine source. Raises PineSyntaxError."""
    lexed = tokenize(source)
    script = Parser(lexed).parse()
    script.version = lexed.version
    return script, lexed
