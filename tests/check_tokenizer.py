"""Differential test: pycpython.tokenizer vs the stdlib tokenize module."""
import io
import sys
import os
import tokenize as std_tokenize
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pycpython import tokenizer as my_tokenizer


def ref_tokens(text):
    try:
        return list(std_tokenize.generate_tokens(io.StringIO(text).readline)), None
    except Exception as e:
        return None, e


def my_tokens(text):
    try:
        return list(my_tokenizer.generate_tokens(io.StringIO(text).readline)), None
    except Exception as e:
        return None, e


def compare(text, name):
    r, re_ = ref_tokens(text)
    m, me = my_tokens(text)
    if re_ is not None or me is not None:
        if type(re_) == type(me) and (re_ is None or str(re_) == str(me)):
            return True, None
        return False, "exc mismatch: ref=%r mine=%r" % (re_, me)
    if len(r) != len(m):
        for i, (a, b) in enumerate(zip(r, m)):
            if a != b:
                return False, "len %d vs %d; first diff at %d:\n  ref=%r\n  me =%r" % (len(r), len(m), i, a, b)
        return False, "len %d vs %d (prefix equal)\n ref tail=%r\n me tail=%r" % (len(r), len(m), r[len(m):][:3], m[len(r):][:3])
    for i, (a, b) in enumerate(zip(r, m)):
        if a != b:
            return False, "diff at %d:\n  ref=%r\n  me =%r" % (i, a, b)
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
        try:
            with open(f, 'rb') as fh:
                data = fh.read()
            enc, _ = std_tokenize.detect_encoding(io.BytesIO(data).readline)
            text = data.decode(enc)
        except Exception as e:
            continue
        try:
            good, msg = compare(text, f)
        except Exception:
            good, msg = False, traceback.format_exc()
        if good:
            ok += 1
        else:
            fail += 1
            print("FAIL", f, msg)
    print("summary: ok=%d fail=%d" % (ok, fail))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
