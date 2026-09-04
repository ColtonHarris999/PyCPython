"""Fuzz / mutation parity check.

Generates random programs (from a small grammar) and token-level mutations
of corpus files, then checks that pycpython and the oracle agree: either both
produce structurally identical code objects, or both raise the same exception
type with the same args.

    python tests/fuzz.py [--iterations N] [--seed S] [FILE...]
"""
import os
import random
import sys
import tokenize
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_codegen import check_source  # noqa: E402

NAMES = ["a", "b", "c", "x", "y", "self", "cls", "_", "print", "len", "range"]
CONSTS = ["0", "1", "2", "255", "256", "-1", "1.5", "1j", "'s'", "b'b'", "None", "True",
          "False", "...", "'a' 'b'", "1_000", "0x10", "f'{x}'", "t'{x}'", "()", "[]", "{}"]
BINOPS = ["+", "-", "*", "/", "//", "%", "**", "<<", ">>", "|", "^", "&", "@"]
CMPOPS = ["<", ">", "==", "!=", "<=", ">=", "is", "is not", "in", "not in"]


class Gen:
    def __init__(self, rng):
        self.rng = rng

    def expr(self, depth=0):
        r = self.rng
        if depth > 3:
            return r.choice(NAMES + CONSTS)
        k = r.randrange(24)
        d = depth + 1
        if k == 0:
            return "(%s %s %s)" % (self.expr(d), r.choice(BINOPS), self.expr(d))
        if k == 1:
            return "(%s %s %s)" % (self.expr(d), r.choice(CMPOPS), self.expr(d))
        if k == 2:
            return "(%s if %s else %s)" % (self.expr(d), self.expr(d), self.expr(d))
        if k == 3:
            return "(%s and %s or not %s)" % (self.expr(d), self.expr(d), self.expr(d))
        if k == 4:
            return "%s(%s)" % (r.choice(NAMES), ", ".join(self.expr(d) for _ in range(r.randrange(4))))
        if k == 5:
            return "%s(%s, *%s, k=%s, **%s)" % (r.choice(NAMES), self.expr(d), self.expr(d),
                                                self.expr(d), self.expr(d))
        if k == 6:
            return "[%s for %s in %s if %s]" % (self.expr(d), r.choice(NAMES[:4]), self.expr(d),
                                                self.expr(d))
        if k == 7:
            return "{%s: %s for %s in %s}" % (self.expr(d), self.expr(d), r.choice(NAMES[:4]),
                                              self.expr(d))
        if k == 8:
            return "(%s for %s in %s)" % (self.expr(d), r.choice(NAMES[:4]), self.expr(d))
        if k == 9:
            return "lambda %s=%s: %s" % (r.choice(NAMES[:4]), self.expr(d), self.expr(d))
        if k == 10:
            return "[%s]" % ", ".join(self.expr(d) for _ in range(r.randrange(5)))
        if k == 11:
            return "(%s,)" % ", ".join(self.expr(d) for _ in range(r.randrange(1, 4)))
        if k == 12:
            return "{%s}" % ", ".join(self.expr(d) for _ in range(r.randrange(1, 4)))
        if k == 13:
            return "{%s}" % ", ".join("%s: %s" % (self.expr(d), self.expr(d)) for _ in range(r.randrange(4)))
        if k == 14:
            return "%s.%s" % (self.expr(d), r.choice(NAMES))
        if k == 15:
            return "%s[%s]" % (self.expr(d), self.expr(d))
        if k == 16:
            return "%s[%s:%s]" % (self.expr(d), self.expr(d), self.expr(d))
        if k == 17:
            return "%s[%s:%s:%s]" % (self.expr(d), self.expr(d), self.expr(d), self.expr(d))
        if k == 18:
            return "(%s := %s)" % (r.choice(NAMES[:4]), self.expr(d))
        if k == 19:
            return "f'{%s!r:>{%s}}'" % (self.expr(d), self.expr(d))
        if k == 20:
            return "(yield %s)" % self.expr(d)
        if k == 21:
            return "await %s" % self.expr(d)
        if k == 22:
            return "-%s" % self.expr(d)
        return r.choice(NAMES + CONSTS)

    def target(self):
        r = self.rng
        k = r.randrange(6)
        if k == 0:
            return "%s, %s" % (r.choice(NAMES[:4]), r.choice(NAMES[:4]))
        if k == 1:
            return "%s.%s" % (r.choice(NAMES[:4]), r.choice(NAMES))
        if k == 2:
            return "%s[%s]" % (r.choice(NAMES[:4]), self.expr(3))
        if k == 3:
            return "[%s, *%s]" % (r.choice(NAMES[:4]), r.choice(NAMES[:4]))
        return r.choice(NAMES[:4])

    def stmt(self, depth=0, indent=""):
        r = self.rng
        k = r.randrange(26) if depth < 3 else r.randrange(5)
        d = depth + 1
        if k == 0:
            return indent + "%s = %s\n" % (self.target(), self.expr())
        if k == 1:
            return indent + "%s %s= %s\n" % (r.choice(NAMES[:4]), r.choice(BINOPS[:12]), self.expr())
        if k == 2:
            return indent + "%s\n" % self.expr()
        if k == 3:
            return indent + "return %s\n" % self.expr()
        if k == 4:
            return indent + r.choice(["pass\n", "break\n", "continue\n", "raise\n",
                                      "raise %s from %s\n" % (self.expr(), self.expr()),
                                      "del %s\n" % self.target(),
                                      "global %s\n" % r.choice(NAMES[:4]),
                                      "nonlocal %s\n" % r.choice(NAMES[:4]),
                                      "assert %s, %s\n" % (self.expr(), self.expr()),
                                      "import os.path as p\n", "from os import (path, sep)\n",
                                      "%s: %s = %s\n" % (self.target(), self.expr(), self.expr()),
                                      "%s: %s\n" % (r.choice(NAMES[:4]), self.expr()),
                                      "yield %s\n" % self.expr(),
                                      "await %s\n" % self.expr()])
        if k == 5:
            return indent + "if %s:\n%s%selse:\n%s" % (self.expr(), self.block(d, indent), indent,
                                                       self.block(d, indent))
        if k == 6:
            return indent + "while %s:\n%s%selse:\n%s" % (self.expr(), self.block(d, indent), indent,
                                                          self.block(d, indent))
        if k == 7:
            return indent + "for %s in %s:\n%s" % (self.target(), self.expr(), self.block(d, indent))
        if k == 8:
            return indent + "def %s(%s, /, %s=%s, *%s, %s, **%s) -> %s:\n%s" % (
                r.choice(NAMES[:4]), "a", "b", self.expr(), "c", "d", "e", self.expr(),
                self.block(d, indent))
        if k == 9:
            return indent + "async def %s(%s):\n%s" % (r.choice(NAMES[:4]), r.choice(NAMES[:4]),
                                                        self.block(d, indent))
        if k == 10:
            return indent + "class %s(%s):\n%s" % (r.choice(NAMES[:4]), self.expr(), self.block(d, indent))
        if k == 11:
            return indent + "try:\n%s%sexcept %s as e:\n%s%sfinally:\n%s" % (
                self.block(d, indent), indent, self.expr(), self.block(d, indent), indent,
                self.block(d, indent))
        if k == 12:
            return indent + "try:\n%s%sexcept* %s:\n%s" % (self.block(d, indent), indent, self.expr(),
                                                            self.block(d, indent))
        if k == 13:
            return indent + "with %s as %s, %s:\n%s" % (self.expr(), self.target(), self.expr(),
                                                        self.block(d, indent))
        if k == 14:
            return indent + "match %s:\n%s    case [%s, *%s] | (%s, _) if %s:\n%s%s    case {'k': %s, **r}:\n%s%s    case %s(%s=%s) as z:\n%s%s    case _:\n%s" % (
                self.expr(), indent, r.choice(NAMES[:4]), r.choice(NAMES[:4]), r.choice(NAMES[:4]),
                self.expr(), self.block(d + 1, indent + "    "), indent, r.choice(NAMES[:4]),
                self.block(d + 1, indent + "    "), indent, r.choice(NAMES), r.choice(NAMES),
                r.choice(NAMES[:4]), self.block(d + 1, indent + "    "), indent,
                self.block(d + 1, indent + "    "))
        if k == 15:
            return indent + "async for %s in %s:\n%s" % (self.target(), self.expr(), self.block(d, indent))
        if k == 16:
            return indent + "async with %s as %s:\n%s" % (self.expr(), self.target(), self.block(d, indent))
        if k == 17:
            return indent + "@%s\ndef %s[T, *Ts, **P](%s: T = %s) -> T:\n%s" % (
                self.expr(), r.choice(NAMES[:4]), r.choice(NAMES[:4]), self.expr(), self.block(d, indent))
        if k == 18:
            return indent + "type %s[T: %s] = %s\n" % (r.choice(NAMES[:4]), self.expr(), self.expr())
        if k == 19:
            return indent + "class %s[T=%s]:\n%s" % (r.choice(NAMES[:4]), self.expr(), self.block(d, indent))
        if k == 20:
            return indent + "for %s in %s:\n%s%selse:\n%s" % (self.target(), self.expr(),
                                                              self.block(d, indent), indent,
                                                              self.block(d, indent))
        if k == 21:
            return indent + "if %s:\n%s%selif %s:\n%s" % (self.expr(), self.block(d, indent), indent,
                                                          self.expr(), self.block(d, indent))
        if k == 22:
            return indent + "def %s():\n%s    \"doc\"\n%s" % (r.choice(NAMES[:4]), indent,
                                                             self.block(d, indent))
        if k == 23:
            return indent + "class %s:\n%s    %s = [%s for %s in %s]\n%s" % (
                r.choice(NAMES[:4]), indent, r.choice(NAMES[:4]), self.expr(), r.choice(NAMES[:4]),
                self.expr(), self.block(d, indent))
        return indent + "%s = %s\n" % (self.target(), self.expr())

    def block(self, depth, indent):
        n = self.rng.randrange(1, 4)
        return "".join(self.stmt(depth, indent + "    ") for _ in range(n))

    def program(self):
        return "".join(self.stmt() for _ in range(self.rng.randrange(1, 6)))


def mutate(src, rng):
    """Token-level mutation of a source file."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return None
    toks = [t for t in toks if t.type not in (tokenize.ENCODING, tokenize.ENDMARKER)]
    if len(toks) < 3:
        return None
    kind = rng.randrange(5)
    i = rng.randrange(len(toks))
    if kind == 0:
        del toks[i]
    elif kind == 1:
        toks.insert(i, toks[rng.randrange(len(toks))])
    elif kind == 2:
        j = rng.randrange(len(toks))
        toks[i], toks[j] = toks[j], toks[i]
    elif kind == 3:
        toks[i] = toks[i]._replace(string=rng.choice(["(", ")", ":", "=", "lambda", "yield",
                                                       "*", "**", ",", "await", "async",
                                                       "1", "'s'", "x", "if", "else"]))
    else:
        toks = toks[:i]
    return tokenize.untokenize([(t.type, t.string) for t in toks])


def main(argv):
    iterations = 300
    seed = 12345
    files = []
    it = iter(argv)
    for a in it:
        if a == "--iterations":
            iterations = int(next(it))
        elif a == "--seed":
            seed = int(next(it))
        else:
            files.append(a)
    rng = random.Random(seed)
    gen = Gen(rng)
    counts = {}
    bad = 0
    sources = []
    for fn in files:
        try:
            with open(fn, encoding="utf-8") as f:
                sources.append((fn, f.read()))
        except (OSError, UnicodeDecodeError):
            pass
    for i in range(iterations):
        if sources and i % 2 == 1:
            fn, src = rng.choice(sources)
            # mutate a random window of the file to keep it fast
            lines = src.splitlines(True)
            start = rng.randrange(max(1, len(lines) - 40))
            window = "".join(lines[start:start + 40])
            m = mutate(window, rng)
            if m is None:
                continue
            src, label = m, "mutation of %s" % fn
        else:
            src, label = gen.program(), "generated"
        for mode in ("exec",) if i % 5 else ("exec", "single"):
            try:
                status, detail = check_source(src, "<fuzz>", mode)
            except Exception as e:  # noqa: BLE001
                status, detail = "error", repr(e)
            counts[status] = counts.get(status, 0) + 1
            if status not in ("tier1", "tier2", "exc-ok"):
                bad += 1
                if bad <= 20:
                    print("==", status, label, mode)
                    print(src if len(src) < 2000 else src[:2000] + "...")
                    print("   ", repr(detail)[:1500])
    total = sum(counts.values())
    print("summary: ok=%d %s" % (total - bad, " ".join("%s=%d" % kv for kv in sorted(counts.items()))))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
