"""Source -> AST front end (port of _PyPegen_run_parser_from_string)."""

import sys

from ..tokenizer import TokState
from . import pegen
from .pegen import (Py_file_input, Py_eval_input, Py_single_input, Py_func_type_input,
                    PyPARSE_DONT_IMPLY_DEDENT, PyPARSE_IGNORE_COOKIE, PyPARSE_BARRY_AS_BDFL,
                    PyPARSE_TYPE_COMMENTS, PyPARSE_ALLOW_INCOMPLETE_INPUT)

# PyCF_* compiler flags (Include/cpython/compile.h)
PyCF_SOURCE_IS_UTF8 = 0x0100
PyCF_DONT_IMPLY_DEDENT = 0x0200
PyCF_ONLY_AST = 0x0400
PyCF_IGNORE_COOKIE = 0x0800
PyCF_TYPE_COMMENTS = 0x1000
PyCF_ALLOW_TOP_LEVEL_AWAIT = 0x2000
PyCF_ALLOW_INCOMPLETE_INPUT = 0x4000
PyCF_OPTIMIZED_AST = 0x8000 | PyCF_ONLY_AST
CO_FUTURE_BARRY_AS_BDFL = 0x400000

PY_MINOR_VERSION = 14

MODE_TO_START = {
    'exec': Py_file_input,
    'eval': Py_eval_input,
    'single': Py_single_input,
    'func_type': Py_func_type_input,
}


def compute_parser_flags(cf_flags):
    parser_flags = 0
    if not cf_flags:
        return 0
    if cf_flags & PyCF_DONT_IMPLY_DEDENT:
        parser_flags |= PyPARSE_DONT_IMPLY_DEDENT
    if cf_flags & PyCF_IGNORE_COOKIE:
        parser_flags |= PyPARSE_IGNORE_COOKIE
    if cf_flags & CO_FUTURE_BARRY_AS_BDFL:
        parser_flags |= PyPARSE_BARRY_AS_BDFL
    if cf_flags & PyCF_TYPE_COMMENTS:
        parser_flags |= PyPARSE_TYPE_COMMENTS
    if cf_flags & PyCF_ALLOW_INCOMPLETE_INPUT:
        parser_flags |= PyPARSE_ALLOW_INCOMPLETE_INPUT
    return parser_flags


def source_as_bytes(source):
    """_Py_SourceAsString: returns (bytes, extra_cf_flags)."""
    if isinstance(source, str):
        data = source.encode('utf-8', 'surrogatepass')
        flags = PyCF_IGNORE_COOKIE
    elif isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        flags = 0
    else:
        raise TypeError("source must be a string, bytes or AST object")
    if b'\x00' in data:
        raise SyntaxError("source code string cannot contain null bytes")
    return data, flags


def run_parser_from_bytes(data, start_rule, filename, cf_flags, feature_version=None):
    """_PyPegen_run_parser_from_string.  *data* is bytes."""
    from .generated_parser import GeneratedParser
    exec_input = start_rule == Py_file_input
    if cf_flags & PyCF_IGNORE_COOKIE:
        tok = TokState.from_utf8(data, exec_input, 0)
    else:
        try:
            tok = TokState.from_string(data, exec_input, 0)
        except SyntaxError as e:
            # _PyTokenizer_raise_init_error: attach the filename attribute only
            e.filename = filename
            raise
        except (LookupError, ValueError, UnicodeDecodeError) as e:
            raise SyntaxError(str(e), (filename, 0, -1, None))
    tok.filename = filename
    parser_flags = compute_parser_flags(cf_flags)
    if feature_version is None or not (cf_flags & PyCF_ONLY_AST):
        feature_version = PY_MINOR_VERSION
    p = GeneratedParser(tok, start_rule, parser_flags, feature_version, filename)
    old = sys.getrecursionlimit()
    if old < 12000:
        sys.setrecursionlimit(12000)
    try:
        return p.run_parser()
    finally:
        sys.setrecursionlimit(old)


def parse(source, filename='<unknown>', mode='exec', flags=0, feature_version=None):
    """Parse *source* (str or bytes) and return a pycpython.pyast tree."""
    if mode not in MODE_TO_START:
        raise ValueError("mode must be 'exec', 'eval', 'single' or 'func_type'")
    data, extra = source_as_bytes(source)
    return run_parser_from_bytes(data, MODE_TO_START[mode], filename, flags | extra,
                                 feature_version)
