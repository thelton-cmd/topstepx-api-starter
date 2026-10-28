"""Errors raised by the Pine Script converter.

``PineSyntaxError`` carries the 1-based line and column of the problem so the CLI and the
dashboard can point at it; ``format_error()`` renders the offending line with a caret under it.
"""

from __future__ import annotations


class PineError(Exception):
    """Base class for every error the converter raises on purpose."""

    def __init__(self, message: str, line: int = 0, col: int = 0):
        super().__init__(message)
        self.message = message
        self.line = int(line or 0)
        self.col = int(col or 0)

    def __str__(self) -> str:
        if self.line:
            return f"line {self.line}, col {self.col}: {self.message}"
        return self.message


class PineSyntaxError(PineError):
    """The script cannot be read or converted (bad syntax, an undeclared name, a misplaced plot...)."""


def format_error(source: str, error: PineError, filename: str = "<pine>") -> str:
    """``file:line:col: error: message`` plus the source line and a caret under the column (ASCII)."""
    head = f"{filename}:{error.line}:{error.col}: error: {error.message}" if error.line else f"{filename}: error: {error.message}"
    lines = source.splitlines()
    if not error.line or error.line > len(lines):
        return _ascii(head)
    text = lines[error.line - 1].replace("\t", "    ")
    col = max(1, error.col)
    return _ascii(f"{head}\n    {text}\n    {' ' * (col - 1)}^")


def _ascii(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")
