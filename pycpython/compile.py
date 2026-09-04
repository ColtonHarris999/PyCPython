"""The public entry point: a reimplementation of the builtin ``compile()``
for CPython 3.14 that never executes code.

Only the "exec", "eval" and "single" modes are supported (no ``ast``
input; PyCF_ONLY_AST is not needed by this library's consumers)."""

import sys

from .parser import parse
from .codegen import compile_ast

_MODES = ("exec", "eval", "single")


def compile_source(source, filename="<string>", mode="exec", flags=0, dont_inherit=False,
                   optimize=-1):
    """Compile *source* (str or bytes) and return a code object.

    Mirrors ``builtins.compile`` for the three source modes.  ``flags``
    accepts the same ``__future__`` and ``PyCF_*`` flags.
    """
    if mode not in _MODES:
        raise ValueError("compile() mode must be 'exec', 'eval' or 'single'")
    if not (-1 <= optimize <= 2):
        raise ValueError("compile(): invalid optimize value")
    if not isinstance(filename, str):
        raise TypeError("compile() filename must be str")
    old_limit = sys.getrecursionlimit()
    if old_limit < 12000:
        sys.setrecursionlimit(12000)
    try:
        mod = parse(source, filename, mode, flags)
        return compile_ast(mod, filename, flags, optimize)
    finally:
        sys.setrecursionlimit(old_limit)
