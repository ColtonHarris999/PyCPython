"""On-device self-test: round-trip .mpy files under MicroPython itself.

Runs on the runtime oracle (the configured unix port) and unchanged on the
target silicon.  It takes pre-built .mpy files as input, since the device has
no mpy-cross, round-trips each through ``mpyc.mpy_format``, and reports the
peak heap consumed so the 64 KB budget can be gated in CI.

    micropython tests/selftest_device.py FILE.mpy...

Any CPython-only construct in the shipped package fails here rather than on
hardware, which is the point.
"""
import gc
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from mpyc import mpy_format  # noqa: E402


def read(path):
    f = open(path, "rb")
    try:
        return f.read()
    finally:
        f.close()


def main(argv):
    if not argv:
        print("usage: selftest_device.py FILE.mpy...")
        return 2
    ok = 0
    bad = 0
    worst = 0
    worst_path = None
    for path in argv:
        data = read(path)
        gc.collect()
        before = gc.mem_free()
        try:
            decoded = mpy_format.decode(data)
            again = mpy_format.encode(decoded)
            good = again == data
        except Exception as e:  # noqa: BLE001
            print("ERROR %s -> %r" % (path, e))
            bad += 1
            continue
        after = gc.mem_free()
        used = before - after
        decoded = None
        again = None
        gc.collect()
        if used > worst:
            worst = used
            worst_path = path
        if good:
            ok += 1
        else:
            bad += 1
            print("MISMATCH %s (%d bytes)" % (path, len(data)))
    print("peak heap: %d bytes for %s" % (worst, worst_path))
    print("summary: ok=%d bad=%d peak_heap=%d" % (ok, bad, worst))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
