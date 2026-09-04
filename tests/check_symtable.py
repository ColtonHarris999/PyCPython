"""Differential test: pycpython.symtable vs the stdlib symtable module."""
import os
import sys
import symtable as std_symtable
import traceback
import warnings

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pycpython import parser as my_parser
from pycpython import symtable as my_symtable
from pycpython.future import future_from_ast

BLOCK_NAMES = {0: 'function', 1: 'class', 2: 'module', 3: 'annotation',
               4: 'type alias', 5: 'type parameters', 6: 'type variable'}


def dump_std(t):
    raw = t._table
    return (raw.name, raw.type, raw.nested, dict(raw.symbols), list(raw.varnames),
            [dump_std(c) for c in t.get_children()])


def dump_mine(ste):
    return (ste.name, ste.type, ste.nested, dict(ste.symbols), list(ste.varnames),
            [dump_mine(c) for c in ste.children])


def first_diff(a, b, path='top'):
    if a[:5] != b[:5]:
        for i, label in enumerate(('name', 'type', 'nested', 'symbols', 'varnames')):
            if a[i] != b[i]:
                if label == 'symbols':
                    ka = set(a[i]); kb = set(b[i])
                    extra = {k: (a[i].get(k), b[i].get(k)) for k in (ka | kb) if a[i].get(k) != b[i].get(k)}
                    return "%s: symbols differ (ref, mine): %r" % (path, extra)
                return "%s: %s %r vs %r" % (path, label, a[i], b[i])
    if len(a[5]) != len(b[5]):
        return "%s: children count %d vs %d (%r vs %r)" % (path, len(a[5]), len(b[5]),
               [c[0] for c in a[5]], [c[0] for c in b[5]])
    for i, (x, y) in enumerate(zip(a[5], b[5])):
        d = first_diff(x, y, "%s/%s[%d]" % (path, x[0], i))
        if d:
            return d
    return None


def compare_one(src, filename):
    warnings.simplefilter('ignore')
    ref_exc = mine_exc = None
    ref = mine = None
    try:
        ref = dump_std(std_symtable.symtable(src, filename, 'exec'))
    except Exception as e:
        ref_exc = e
    try:
        mod = my_parser.parse(src, filename, 'exec')
        fut = future_from_ast(mod, filename)
        st = my_symtable.build(mod, filename, fut)
        mine = dump_mine(st.top)
    except Exception as e:
        mine_exc = e
    if ref_exc is not None or mine_exc is not None:
        if type(ref_exc) is type(mine_exc) and (
                not isinstance(ref_exc, SyntaxError) or
                (ref_exc.args[0] == mine_exc.args[0] and ref_exc.lineno == mine_exc.lineno
                 and ref_exc.offset == mine_exc.offset and ref_exc.end_lineno == mine_exc.end_lineno
                 and ref_exc.end_offset == mine_exc.end_offset)):
            return True, None
        return False, "exception mismatch:\n  ref=%r %r\n  me =%r %r" % (
            ref_exc, getattr(ref_exc, 'args', None), mine_exc, getattr(mine_exc, 'args', None))
    d = first_diff(ref, mine)
    if d:
        return False, d
    return True, None


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
            good, msg = compare_one(data, f)
        except Exception:
            good, msg = False, traceback.format_exc()
        if good:
            ok += 1
        else:
            fail += 1
            print("FAIL", f, msg)
            sys.stdout.flush()
    print("summary: ok=%d fail=%d" % (ok, fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
