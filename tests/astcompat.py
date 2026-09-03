"""Convert pycpython.pyast trees into stdlib ``ast`` trees (test tooling)."""
import ast

from pycpython import pyast


def to_stdlib(node):
    if isinstance(node, list):
        return [to_stdlib(x) for x in node]
    if not isinstance(node, pyast.AST):
        return node
    cls = getattr(ast, type(node).__name__)
    kwargs = {}
    for f in node._fields:
        kwargs[f] = to_stdlib(getattr(node, f))
    for a in node._attributes:
        v = getattr(node, a, None)
        if v is not None:
            kwargs[a] = v
    return cls(**kwargs)


def from_stdlib(node):
    """Convert a stdlib ast tree into a pycpython.pyast tree."""
    if isinstance(node, list):
        return [from_stdlib(x) for x in node]
    if not isinstance(node, ast.AST):
        return node
    name = type(node).__name__
    cls = getattr(pyast, name)
    if not cls._fields and not cls._attributes:
        return getattr(pyast, name + '_singleton', None) or cls()
    kwargs = {}
    for f in node._fields:
        kwargs[f] = from_stdlib(getattr(node, f, None))
    for a in node._attributes:
        kwargs[a] = getattr(node, a, None)
    return cls(**kwargs)
