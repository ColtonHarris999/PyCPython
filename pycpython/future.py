"""Port of Python/future.c: detect ``from __future__ import ...`` statements."""

from . import pyast as ast
from .errors import SourceLocation, raise_syntax_error_at

CO_FUTURE_DIVISION = 0x20000
CO_FUTURE_ABSOLUTE_IMPORT = 0x40000
CO_FUTURE_WITH_STATEMENT = 0x80000
CO_FUTURE_PRINT_FUNCTION = 0x100000
CO_FUTURE_UNICODE_LITERALS = 0x200000
CO_FUTURE_BARRY_AS_BDFL = 0x400000
CO_FUTURE_GENERATOR_STOP = 0x800000
CO_FUTURE_ANNOTATIONS = 0x1000000

PyCF_MASK = (CO_FUTURE_DIVISION | CO_FUTURE_ABSOLUTE_IMPORT | CO_FUTURE_WITH_STATEMENT
             | CO_FUTURE_PRINT_FUNCTION | CO_FUTURE_UNICODE_LITERALS
             | CO_FUTURE_BARRY_AS_BDFL | CO_FUTURE_GENERATOR_STOP | CO_FUTURE_ANNOTATIONS)

_NO_EFFECT = {"nested_scopes", "generators", "division", "absolute_import",
              "with_statement", "print_function", "unicode_literals", "generator_stop"}


class FutureFeatures:
    __slots__ = ('ff_features', 'ff_location')

    def __init__(self):
        self.ff_features = 0
        self.ff_location = SourceLocation(-1, -1, -1, -1)


def get_docstring(body):
    """_PyAST_GetDocString: the docstring constant node value, or None."""
    if not body:
        return None
    st = body[0]
    if isinstance(st, ast.Expr):
        e = st.value
        if isinstance(e, ast.Constant) and type(e.value) is str:
            return e.value
    return None


def _check_features(ff, s, filename):
    for name in s.names:
        feature = name.name
        if feature in _NO_EFFECT:
            continue
        elif feature == "barry_as_FLUFL":
            ff.ff_features |= CO_FUTURE_BARRY_AS_BDFL
        elif feature == "annotations":
            ff.ff_features |= CO_FUTURE_ANNOTATIONS
        elif feature == "braces":
            raise_syntax_error_at("not a chance", filename, name)
        else:
            raise_syntax_error_at("future feature %.100s is not defined" % feature,
                                  filename, name)


def future_from_ast(mod, filename):
    ff = FutureFeatures()
    if not isinstance(mod, (ast.Module, ast.Interactive)):
        return ff
    body = mod.body
    n = len(body)
    if n == 0:
        return ff
    i = 0
    if get_docstring(body) is not None:
        i += 1
    while i < n:
        s = body[i]
        if isinstance(s, ast.ImportFrom) and s.level == 0:
            modname = s.module
            if modname is not None and modname == "__future__":
                _check_features(ff, s, filename)
                ff.ff_location = SourceLocation.from_node(s)
            else:
                return ff
        else:
            return ff
        i += 1
    return ff
