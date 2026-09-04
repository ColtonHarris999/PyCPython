"""Source locations and SyntaxError construction helpers shared by the
compiler stages (symtable, codegen, flowgraph)."""

import warnings


class SourceLocation:
    """_Py_SourceLocation."""
    __slots__ = ('lineno', 'end_lineno', 'col_offset', 'end_col_offset')

    def __init__(self, lineno, end_lineno, col_offset, end_col_offset):
        self.lineno = lineno
        self.end_lineno = end_lineno
        self.col_offset = col_offset
        self.end_col_offset = end_col_offset

    @classmethod
    def from_node(cls, n):
        return cls(n.lineno, n.end_lineno, n.col_offset, n.end_col_offset)

    def __eq__(self, other):
        return (isinstance(other, SourceLocation) and self.lineno == other.lineno
                and self.end_lineno == other.end_lineno
                and self.col_offset == other.col_offset
                and self.end_col_offset == other.end_col_offset)

    def __hash__(self):
        return hash((self.lineno, self.end_lineno, self.col_offset, self.end_col_offset))

    def __repr__(self):
        return "Loc(%d:%d-%d:%d)" % (self.lineno, self.col_offset,
                                     self.end_lineno, self.end_col_offset)

    def as_tuple(self):
        return (self.lineno, self.end_lineno, self.col_offset, self.end_col_offset)


NO_LOCATION = SourceLocation(-1, -1, -1, -1)
NEXT_LOCATION = SourceLocation(-2, -2, -2, -2)


def _program_line(filename, lineno):
    from .parser.pegen import _program_text
    try:
        return _program_text(filename, lineno, None)
    except Exception:
        return None


def make_syntax_error(msg, filename, lineno, col_offset, end_lineno, end_col_offset,
                      exc_type=SyntaxError):
    """PyErr_RangedSyntaxLocationObject: build a SyntaxError with the given
    (1-based) column information.  The error text comes from the file on
    disk, if it exists."""
    text = _program_line(filename, lineno) if lineno and lineno > 0 else None
    exc = exc_type(msg)
    exc.filename = filename
    exc.lineno = lineno
    exc.offset = col_offset
    exc.text = text
    exc.end_lineno = end_lineno
    exc.end_offset = end_col_offset
    exc.print_file_and_line = None
    return exc


def raise_syntax_error_at(msg, filename, node_or_loc, exc_type=SyntaxError):
    """SET_ERROR_LOCATION / PyErr_RangedSyntaxLocationObject with +1 on
    the column offsets."""
    loc = node_or_loc
    raise make_syntax_error(msg, filename, loc.lineno, loc.col_offset + 1,
                            loc.end_lineno, loc.end_col_offset + 1, exc_type)


def emit_syntax_warning(msg, filename, lineno, col_offset, end_lineno, end_col_offset):
    """_PyErr_EmitSyntaxWarning: warn, or turn the warning into a SyntaxError
    when warnings are errors."""
    try:
        warnings.warn_explicit(msg, SyntaxWarning, filename, lineno)
    except SyntaxWarning:
        raise make_syntax_error(msg, filename, lineno, col_offset, end_lineno,
                                end_col_offset)
    return 0


def raise_syntax_error_full(msg, filename, loc):
    """_PyErr_RaiseSyntaxError as used by the compiler (compile.c): the
    SyntaxError carries the full ``(msg, (filename, lineno, offset, text,
    end_lineno, end_offset))`` args tuple, with 1-based columns."""
    lineno = loc.lineno
    text = _program_line(filename, lineno) if lineno and lineno > 0 else None
    raise SyntaxError(msg, (filename, lineno, loc.col_offset + 1, text,
                            loc.end_lineno, loc.end_col_offset + 1))


def emit_syntax_warning_full(msg, filename, loc):
    """_PyErr_EmitSyntaxWarning: emit a SyntaxWarning; when warnings are
    errors the SyntaxWarning is replaced by a SyntaxError with full args."""
    try:
        warnings.warn_explicit(msg, SyntaxWarning, filename, loc.lineno)
    except SyntaxWarning:
        raise_syntax_error_full(msg, filename, loc)
    return 0
