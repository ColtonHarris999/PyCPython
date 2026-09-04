"""The public entry point: a reimplementation of the builtin ``compile()``
for CPython 3.14 that never executes code.

Only the "exec", "eval" and "single" modes are supported.  ``compile_source``
returns a code object; ``parse_to_ast`` mirrors the ``PyCF_ONLY_AST`` path of
``Py_CompileStringObject`` (i.e. ``ast.parse``) and returns the module tree."""

import sys

from .parser import parse
from .codegen import compile_ast
from . import future as _future
from . import ast_preprocess as _ast_preprocess

PyCF_ONLY_AST = 0x0400
PyCF_OPTIMIZED_AST = 0x8000 | PyCF_ONLY_AST

_MODES = ("exec", "eval", "single")


def _check_args(mode, optimize, filename):
    if mode not in _MODES:
        raise ValueError("compile() mode must be 'exec', 'eval' or 'single'")
    if not (-1 <= optimize <= 2):
        raise ValueError("compile(): invalid optimize value")
    if not isinstance(filename, str):
        raise TypeError("compile() filename must be str")


def parse_to_ast(source, filename="<string>", mode="exec", flags=0, optimize=-1):
    """Parse *source* and return the (pycpython) AST, exactly as ``ast.parse``
    would: the tree is run through ``_PyCompile_AstPreprocess`` which
    validates ``from __future__`` imports and, when ``PyCF_OPTIMIZED_AST`` is
    requested, applies the AST optimizer (constant folding)."""
    _check_args(mode, optimize, filename)
    old_limit = sys.getrecursionlimit()
    if old_limit < 12000:
        sys.setrecursionlimit(12000)
    try:
        mod = parse(source, filename, mode, flags)
        syntax_check_only = (flags & PyCF_OPTIMIZED_AST) != PyCF_OPTIMIZED_AST
        ff = _future.future_from_ast(mod, filename)
        merged = ff.ff_features | flags
        opt = optimize if optimize != -1 else sys.flags.optimize
        _ast_preprocess.preprocess(mod, filename, opt, merged, syntax_check_only, 0)
        return mod
    finally:
        sys.setrecursionlimit(old_limit)


def compile_source(source, filename="<string>", mode="exec", flags=0, dont_inherit=False,
                   optimize=-1):
    """Compile *source* (str or bytes) and return a code object.

    Mirrors ``builtins.compile`` for the three source modes.  ``flags``
    accepts the same ``__future__`` and ``PyCF_*`` flags.
    """
    _check_args(mode, optimize, filename)
    old_limit = sys.getrecursionlimit()
    if old_limit < 12000:
        sys.setrecursionlimit(12000)
    try:
        mod = parse(source, filename, mode, flags)
        return compile_ast(mod, filename, flags, optimize)
    finally:
        sys.setrecursionlimit(old_limit)
