"""Tier 1 / Tier 2 differential check: compile every file with the oracle
``compile()`` and with pycpython, compare the resulting code objects.

Usage: python tests/check_codegen.py [-v] [--mode exec|eval|single] PATH...
PATH may be files or directories (searched recursively for *.py).

Tier 1: structural equality (bytecode, consts recursively, names, locals,
        flags, arg counts, stack size, exception table, line table).
Tier 2: marshal.dumps() byte equality.
"""
import sys
import os
import io
import dis
import marshal
import traceback
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pycpython.compile import compile_source  # noqa: E402

CODE_ATTRS = ('co_argcount', 'co_posonlyargcount', 'co_kwonlyargcount', 'co_nlocals',
              'co_stacksize', 'co_flags', 'co_code', 'co_names', 'co_varnames',
              'co_freevars', 'co_cellvars', 'co_name', 'co_qualname', 'co_firstlineno',
              'co_exceptiontable', 'co_linetable')

_code_type = type(compile_source.__code__)


def const_key(o):
    t = type(o)
    if t is _code_type:
        return ('code', id(o))
    if t is tuple:
        return (t, tuple(const_key(x) for x in o))
    if t is frozenset:
        return (t, frozenset(const_key(x) for x in o))
    if t is float and o == 0.0:
        return (t, o, str(o).startswith('-'))
    if t is complex:
        return (t, o, str(o.real).startswith('-'), str(o.imag).startswith('-'))
    if t is slice:
        return (t, const_key(o.start), const_key(o.stop), const_key(o.step))
    return (t, o)


def diff_code(a, b, path, out):
    """Append (path, attr, mine, ref) descriptions of differences."""
    for attr in CODE_ATTRS:
        va, vb = getattr(a, attr), getattr(b, attr)
        if va != vb:
            out.append((path, attr, va, vb))
    if len(a.co_consts) != len(b.co_consts):
        out.append((path, 'co_consts(len)', len(a.co_consts), len(b.co_consts)))
        return
    for i, (ca, cb) in enumerate(zip(a.co_consts, b.co_consts)):
        if type(ca) is _code_type and type(cb) is _code_type:
            diff_code(ca, cb, path + '/' + cb.co_qualname, out)
        elif type(ca) is _code_type or type(cb) is _code_type:
            out.append((path, 'co_consts[%d]' % i, ca, cb))
        elif const_key(ca) != const_key(cb):
            out.append((path, 'co_consts[%d]' % i, ca, cb))


def exc_repr(e):
    return (type(e).__name__, getattr(e, 'args', None))


def check_source(src, filename, mode, optimize=-1):
    """Returns (status, detail) where status in
    ('tier2', 'tier1', 'fail', 'error', 'exc-ok', 'exc-mismatch')."""
    ref_exc = None
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        try:
            ref = compile(src, filename, mode, 0, True, optimize)
        except (SyntaxError, ValueError, TypeError, RecursionError, MemoryError) as e:
            ref_exc = e
            ref = None
        try:
            mine = compile_source(src, filename, mode, 0, True, optimize)
            mine_exc = None
        except (SyntaxError, ValueError, TypeError, RecursionError, MemoryError) as e:
            mine_exc = e
            mine = None
        except Exception:
            return 'error', traceback.format_exc()
    if ref_exc is not None or mine_exc is not None:
        if ref_exc is not None and mine_exc is not None:
            if exc_repr(ref_exc) == exc_repr(mine_exc):
                return 'exc-ok', exc_repr(ref_exc)
            return 'exc-mismatch', (exc_repr(mine_exc), exc_repr(ref_exc))
        if mine_exc is not None:
            return 'exc-mismatch', (exc_repr(mine_exc), None, traceback.format_exception(mine_exc)[-3:])
        return 'exc-mismatch', (None, exc_repr(ref_exc))
    diffs = []
    diff_code(mine, ref, ref.co_qualname, diffs)
    if diffs:
        return 'fail', diffs
    # Tier 2: hold references to the filename objects so both have refcount > 1
    fa = mine.co_filename
    fb = ref.co_filename
    ma = marshal.dumps(mine)
    mb = marshal.dumps(ref)
    del fa, fb
    if ma == mb:
        return 'tier2', None
    for i, (x, y) in enumerate(zip(ma, mb)):
        if x != y:
            return 'tier1', (i, ma[max(0, i - 40):i + 20], mb[max(0, i - 40):i + 20])
    return 'tier1', (len(ma), len(mb))


def describe_diff(d):
    path, attr, va, vb = d
    s = "  %s: %s differs\n" % (path, attr)
    if attr == 'co_code':
        pass
    else:
        s += "    mine: %r\n    ref:  %r\n" % (va, vb)
    return s


def iter_files(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for f in sorted(files):
                    if f.endswith('.py'):
                        yield os.path.join(root, f)
        else:
            yield p


def main(argv):
    verbose = False
    mode = 'exec'
    paths = []
    it = iter(argv)
    for a in it:
        if a == '-v':
            verbose = True
        elif a == '--mode':
            mode = next(it)
        else:
            paths.append(a)
    counts = {}
    for fn in iter_files(paths):
        try:
            with open(fn, 'rb') as f:
                src = f.read()
        except OSError:
            continue
        try:
            status, detail = check_source(src, fn, mode)
        except Exception:
            status, detail = 'error', traceback.format_exc()
        counts[status] = counts.get(status, 0) + 1
        if status in ('tier2', 'exc-ok'):
            if verbose:
                print(status, fn)
        else:
            print(status, fn)
            if status == 'fail':
                for d in detail[:5]:
                    print(describe_diff(d), end='')
            elif status == 'tier1':
                print("   ", detail)
            else:
                print("   ", detail if isinstance(detail, str) else repr(detail)[:2000])
        sys.stdout.flush()
    print("summary:", " ".join("%s=%d" % kv for kv in sorted(counts.items())))


if __name__ == '__main__':
    main(sys.argv[1:])
