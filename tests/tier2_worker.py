"""Worker for the Tier 2 (byte-exact marshal) check.

Reads file paths from stdin (one per line), compiles each with either the
reference ``compile()`` (--which ref) or pycpython (--which mine) and prints
one result line per file:  path<TAB>status<TAB>sha256<TAB>size

Both workers import exactly the same modules so that the interpreter's
interned-string table (which influences constant sharing inside code
objects) evolves identically in both processes.
"""
import sys
import os
import hashlib
import marshal
import warnings
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pycpython.compile  # noqa: E402  (imported by both workers)
from pycpython.compile import compile_source  # noqa: E402


def main():
    which = sys.argv[sys.argv.index('--which') + 1]
    mode = 'exec'
    if '--mode' in sys.argv:
        mode = sys.argv[sys.argv.index('--mode') + 1]
    dump_dir = None
    if '--dump-dir' in sys.argv:
        dump_dir = sys.argv[sys.argv.index('--dump-dir') + 1]
    out = sys.stdout
    for line in sys.stdin:
        fn = line.rstrip('\n')
        if not fn:
            continue
        try:
            with open(fn, 'rb') as f:
                src = f.read()
        except OSError as e:
            out.write("%s\tioerror\t%s\t0\n" % (fn, e))
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                if which == 'ref':
                    co = compile(src, fn, mode, 0, True, -1)
                else:
                    co = compile_source(src, fn, mode, 0, True, -1)
        except (SyntaxError, ValueError, TypeError, RecursionError, MemoryError) as e:
            key = hashlib.sha256(repr((type(e).__name__, e.args)).encode()).hexdigest()[:16]
            out.write("%s\texc:%s\t%s\t0\n" % (fn, type(e).__name__, key))
            out.flush()
            continue
        except Exception:
            out.write("%s\terror\t%s\t0\n" % (fn, traceback.format_exc().splitlines()[-1]))
            out.flush()
            continue
        fname = co.co_filename  # keep an extra reference (see check_codegen)
        data = marshal.dumps(co)
        del fname
        digest = hashlib.sha256(data).hexdigest()
        if dump_dir:
            with open(os.path.join(dump_dir, digest[:16] + '.' + which), 'wb') as f:
                f.write(data)
        out.write("%s\tok\t%s\t%d\n" % (fn, digest, len(data)))
        out.flush()


if __name__ == '__main__':
    main()
