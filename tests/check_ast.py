"""Tier 0 differential test: pycpython parser vs ast.parse, using ast.compare."""
import ast
import io
import os
import sys
import tokenize
import traceback
import warnings

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pycpython import parser as my_parser
from astcompat import to_stdlib


def compare_one(src, filename, mode, positions=True):
    warnings.simplefilter('ignore')
    ref_exc = mine_exc = None
    ref = mine = None
    try:
        ref = ast.parse(src, filename, mode)
    except Exception as e:
        ref_exc = e
    try:
        mine = to_stdlib(my_parser.parse(src, filename, mode))
    except Exception as e:
        mine_exc = e
    if ref_exc is not None or mine_exc is not None:
        if type(ref_exc) is type(mine_exc) and (
                not isinstance(ref_exc, SyntaxError) or
                (ref_exc.args == mine_exc.args)):
            return True, None
        return False, "exception mismatch:\n  ref=%r %r\n  me =%r %r" % (
            ref_exc, getattr(ref_exc, 'args', None), mine_exc, getattr(mine_exc, 'args', None))
    if ast.compare(ref, mine, compare_attributes=positions):
        return True, None
    return False, first_diff(ref, mine, positions)


def first_diff(a, b, positions, path='root'):
    if type(a) is not type(b):
        return "%s: type %s vs %s" % (path, type(a).__name__, type(b).__name__)
    if isinstance(a, list):
        if len(a) != len(b):
            return "%s: len %d vs %d" % (path, len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, positions, "%s[%d]" % (path, i))
            if d:
                return d
        return None
    if isinstance(a, ast.AST):
        for f in a._fields:
            d = first_diff(getattr(a, f, None), getattr(b, f, None), positions, "%s.%s" % (path, f))
            if d:
                return d
        if positions:
            for f in a._attributes:
                if getattr(a, f, None) != getattr(b, f, None):
                    return "%s.%s: %r vs %r" % (path, f, getattr(a, f, None), getattr(b, f, None))
        return None
    if a != b or type(a) is not type(b):
        return "%s: %r vs %r" % (path, a, b)
    return None


def main(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, fs in os.walk(p):
                for f in fs:
                    if f.endswith('.py'):
                        files.append(os.path.join(root, f))
        else:
            files.append(p)
    ok = fail = 0
    for f in sorted(files):
        with open(f, 'rb') as fh:
            data = fh.read()
        try:
            good, msg = compare_one(data, f, 'exec')
        except Exception:
            good, msg = False, traceback.format_exc()
        if good:
            ok += 1
        else:
            fail += 1
            print("FAIL", f, msg)
            sys.stdout.flush()
    print("ok=%d fail=%d" % (ok, fail))


if __name__ == '__main__':
    main(sys.argv[1:])
