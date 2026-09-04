"""Tier 2 check: compile every file in two fresh, identically initialized
interpreter processes (one using the reference compile(), one using
pycpython) and compare marshal.dumps() of the results byte for byte.

Usage: python tests/check_tier2.py [-v] [--mode exec|eval|single] [--dump-dir D] PATH...
"""
import sys
import os
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_codegen import iter_files  # noqa: E402


def run_worker(which, files, mode, dump_dir):
    cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        'tier2_worker.py'), '--which', which, '--mode', mode]
    if dump_dir:
        cmd += ['--dump-dir', dump_dir]
    p = subprocess.run(cmd, input="\n".join(files) + "\n", capture_output=True, text=True)
    results = {}
    for line in p.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) == 4:
            results[parts[0]] = (parts[1], parts[2], int(parts[3]))
    if p.returncode != 0:
        sys.stderr.write(p.stderr[-2000:])
    return results


def main(argv):
    verbose = False
    mode = 'exec'
    dump_dir = None
    paths = []
    it = iter(argv)
    for a in it:
        if a == '-v':
            verbose = True
        elif a == '--mode':
            mode = next(it)
        elif a == '--dump-dir':
            dump_dir = next(it)
        else:
            paths.append(a)
    files = list(iter_files(paths))
    ref = run_worker('ref', files, mode, dump_dir)
    mine = run_worker('mine', files, mode, dump_dir)
    counts = {}
    for fn in files:
        r = ref.get(fn)
        m = mine.get(fn)
        if r is None or m is None:
            status = 'missing'
        elif r == m:
            status = 'tier2' if r[0] == 'ok' else ('exc-ok' if r[0].startswith('exc') else r[0])
        else:
            status = 'mismatch'
        counts[status] = counts.get(status, 0) + 1
        if status in ('tier2', 'exc-ok'):
            if verbose:
                print(status, fn)
        else:
            print(status, fn, 'mine=%r' % (m,), 'ref=%r' % (r,))
    print("summary:", " ".join("%s=%d" % kv for kv in sorted(counts.items())))


if __name__ == '__main__':
    main(sys.argv[1:])
