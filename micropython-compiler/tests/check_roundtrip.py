"""Byte-exact round-trip of the .mpy container against the oracle.

For every .py file in the corpus: compile it with the pinned mpy-cross, decode
the result with mpyc.mpy_format, re-encode it, and require the bytes to be
identical.  This validates the container, the qstr table, the constant object
table and the raw-code tree without needing the emitter.

    python tests/check_roundtrip.py --mpy-cross PATH PATH...
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from mpyc import mpy_format  # noqa: E402


def iter_files(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                for f in sorted(files):
                    if f.endswith(".py"):
                        yield os.path.join(root, f)
        elif p.endswith(".py"):
            yield p


def check_one(mpy_cross, path, tmpdir):
    out = os.path.join(tmpdir, "out.mpy")
    if os.path.exists(out):
        os.unlink(out)
    p = subprocess.run([mpy_cross, "-o", out, path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return "oracle-reject", (p.stderr.strip().splitlines() or [""])[-1][:120]
    with open(out, "rb") as f:
        ref = f.read()
    try:
        decoded = mpy_format.decode(ref)
    except mpy_format.MpyError as e:
        return "decode-error", str(e)
    try:
        again = mpy_format.encode(decoded)
    except mpy_format.MpyError as e:
        return "encode-error", str(e)
    if again != ref:
        n = min(len(ref), len(again))
        at = n
        for i in range(n):
            if ref[i] != again[i]:
                at = i
                break
        return "mismatch", ("len ref=%d mine=%d, first difference at byte %d: "
                            "ref=%r mine=%r" % (
                                len(ref), len(again), at,
                                ref[at:at + 8], again[at:at + 8]))
    # Re-decoding our own output must agree structurally too.
    if mpy_format.decode(again).small_int_bits != decoded.small_int_bits:
        return "mismatch", "small_int_bits changed across re-decode"
    return "ok", None


def main(argv):
    mpy_cross = None
    paths = []
    verbose = False
    it = iter(argv)
    for a in it:
        if a == "--mpy-cross":
            mpy_cross = next(it)
        elif a == "-v":
            verbose = True
        else:
            paths.append(a)
    if mpy_cross is None:
        print("ERROR: --mpy-cross PATH is required (build it from the v1.29.0 tag)")
        return 2
    ver = subprocess.run([mpy_cross, "--version"], capture_output=True, text=True)
    banner = (ver.stdout + ver.stderr).strip()
    print("oracle: %s" % banner)
    if "v1.29.0" not in banner:
        print("ERROR: oracle must be mpy-cross from tag v1.29.0")
        return 2
    counts = {}
    tmpdir = tempfile.mkdtemp()
    for path in iter_files(paths):
        try:
            status, detail = check_one(mpy_cross, path, tmpdir)
        except Exception as e:  # noqa: BLE001
            status, detail = "error", repr(e)
        counts[status] = counts.get(status, 0) + 1
        if status != "ok" or verbose:
            print("%-13s %s" % (status, path))
            if detail:
                print("              %s" % detail)
            sys.stdout.flush()
    print("summary: " + " ".join(
        "%s=%d" % kv for kv in sorted(counts.items())))
    bad = sum(v for k, v in counts.items()
              if k not in ("ok", "oracle-reject"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
