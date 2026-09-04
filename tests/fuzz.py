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
CLASS_NAMES = ["y", "self", "cls", "print", "len", "range"]
CMPOPS = ["<", ">", "==", "!=", "<=", ">=", "is", "is not", "in", "not in"]


class Gen:
    """Random program generator.

    With ``valid=True`` the generator tracks scopes (module / function /
    async function / class / lambda / comprehension), loops and "strict"
    contexts (annotations, type parameter bounds) so that most programs are
    accepted by the compiler and exercise code generation; with
    ``valid=False`` it emits anything the grammar allows, which mostly
    exercises error parity."""

    def __init__(self, rng, valid=True):
        self.rng = rng
        self.valid = valid
        self.scopes = ["module"]
        self.strict = 0
        self.loops = 0
        self.no_control = 0
        self.comp_depth = 0

    # -- context predicates ---------------------------------------------

    def scope(self):
        return self.scopes[-1]

    def enclosing_function(self):
        for sc in reversed(self.scopes):
            if sc != "comp":
                return sc
        return "module"

    def can_yield(self):
        return not self.valid or (self.strict == 0 and self.scope() in ("func", "async", "lambda"))

    def can_await(self):
        return not self.valid or (self.strict == 0 and self.enclosing_function() == "async"
                                  and self.scope() != "lambda")

    def can_walrus(self):
        return not self.valid or (self.strict == 0 and self.comp_depth == 0)

    def push(self, sc):
        self.scopes.append(sc)

    def pop(self):
        self.scopes.pop()

    def strict_expr(self, depth=0):
        self.strict += 1
        try:
            return self.expr(depth)
        finally:
            self.strict -= 1

    # -- expressions ------------------------------------------------------

    def comp(self, fmt, depth):
        """A comprehension: *fmt* is filled with sub-expressions generated
        in a fresh comprehension scope."""
        self.push("comp")
        self.comp_depth += 1
        try:
            return fmt(depth)
        finally:
            self.comp_depth -= 1
            self.pop()

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
            return self.comp(lambda d: "[%s for %s in %s if %s]" % (
                self.expr(d), r.choice(NAMES[:4]), self.expr(d), self.expr(d)), d)
        if k == 7:
            return self.comp(lambda d: "{%s: %s for %s in %s}" % (
                self.expr(d), self.expr(d), r.choice(NAMES[:4]), self.expr(d)), d)
        if k == 8:
            return self.comp(lambda d: "(%s for %s in %s)" % (
                self.expr(d), r.choice(NAMES[:4]), self.expr(d)), d)
        if k == 9:
            default = self.expr(d)
            self.push("lambda")
            try:
                body = self.expr(d)
            finally:
                self.pop()
            return "(lambda %s=%s: %s)" % (r.choice(NAMES[:4]), default, body)
        if k == 10:
            return "[%s]" % ", ".join(self.expr(d) for _ in range(r.randrange(5)))
        if k == 11:
            return "(%s,)" % ", ".join(self.expr(d) for _ in range(r.randrange(1, 4)))
        if k == 12:
            return "{%s}" % ", ".join(self.expr(d) for _ in range(r.randrange(1, 4)))
        if k == 13:
            return "{%s}" % ", ".join("%s: %s" % (self.expr(d), self.expr(d)) for _ in range(r.randrange(4)))
        if k == 14:
            return "(%s).%s" % (self.expr(d), r.choice(NAMES))
        if k == 15:
            return "%s[%s]" % (self.expr(d), self.expr(d))
        if k == 16:
            return "%s[%s:%s]" % (self.expr(d), self.expr(d), self.expr(d))
        if k == 17:
            return "%s[%s:%s:%s]" % (self.expr(d), self.expr(d), self.expr(d), self.expr(d))
        if k == 18 and self.can_walrus():
            return "(%s := %s)" % (r.choice(NAMES[:4]), self.expr(d))
        if k == 19:
            return "f'{(%s)!r:>{(%s)}}'" % (self.expr(d), self.expr(d))
        if k == 20 and self.can_yield():
            return "(yield %s)" % self.expr(d)
        if k == 21 and self.can_await():
            return "(await (%s))" % self.expr(d)
        if k == 22:
            return "-%s" % self.expr(d)
        return r.choice(NAMES + CONSTS)

    def target(self, simple=False, star=True):
        r = self.rng
        k = r.randrange(6)
        if k == 0 and not simple:
            return "%s, %s" % (r.choice(NAMES[:4]), r.choice(NAMES[:4]))
        if k == 1:
            return "%s.%s" % (r.choice(NAMES[:4]), r.choice(NAMES))
        if k == 2:
            return "%s[%s]" % (r.choice(NAMES[:4]), self.expr(3))
        if k == 3 and not simple and (star or not self.valid):
            return "[%s, *%s]" % (r.choice(NAMES[:4]), r.choice(NAMES[:4]))
        return r.choice(NAMES[:4])

    # -- statements -------------------------------------------------------

    def simple_stmt(self):
        r = self.rng
        choices = ["pass\n", "raise\n",
                   "raise %s from %s\n" % (self.expr(), self.expr()),
                   "del %s\n" % self.target(star=False),
                   "assert %s, %s\n" % (self.expr(), self.expr()),
                   "import os.path as p\n", "from os import (path, sep)\n",
                   "%s: %s = %s\n" % (self.target(simple=True), self.strict_expr(), self.expr()),
                   "%s: %s\n" % (r.choice(NAMES[:4]), self.strict_expr())]
        if self.loops and not self.no_control:
            choices += ["break\n", "continue\n"]
        if not self.valid:
            choices += ["global %s\n" % r.choice(NAMES[:4]),
                        "nonlocal %s\n" % r.choice(NAMES[:4])]
        if self.can_yield():
            choices.append("yield %s\n" % self.expr())
        if self.can_await():
            choices.append("await (%s)\n" % self.expr())
        return r.choice(choices)

    def func_body(self, sc, depth, indent, docstring=False):
        """A function body block in scope *sc* (loops/control state reset)."""
        self.push(sc)
        saved = (self.loops, self.no_control)
        self.loops = self.no_control = 0
        try:
            body = self.block(depth, indent)
            if docstring:
                body = indent + "    \"doc\"\n" + body
            return body
        finally:
            self.loops, self.no_control = saved
            self.pop()

    def class_body(self, depth, indent, prefix=""):
        self.push("class")
        saved = (self.loops, self.no_control)
        self.loops = self.no_control = 0
        try:
            return prefix + self.block(depth, indent)
        finally:
            self.loops, self.no_control = saved
            self.pop()

    def loop_body(self, depth, indent):
        self.loops += 1
        try:
            return self.block(depth, indent)
        finally:
            self.loops -= 1

    def no_control_body(self, depth, indent):
        self.no_control += 1
        try:
            return self.block(depth, indent)
        finally:
            self.no_control -= 1

    def return_stmt(self):
        sc = self.scope()
        if self.valid and sc not in ("func", "async"):
            return None
        if self.valid and (sc == "async" or self.no_control):
            # async generators cannot return a value; except* forbids return
            return "return\n" if not self.no_control else None
        return "return %s\n" % self.expr()

    def stmt(self, depth=0, indent=""):
        r = self.rng
        k = r.randrange(28) if depth < 3 else r.randrange(5)
        d = depth + 1
        in_async = self.enclosing_function() == "async" and self.scope() != "class"
        if k == 0:
            return indent + "%s = %s\n" % (self.target(), self.expr())
        if k == 1:
            return indent + "%s %s= %s\n" % (r.choice(NAMES[:4]), r.choice(BINOPS[:12]), self.expr())
        if k == 2:
            return indent + "%s\n" % self.expr()
        if k == 3:
            st = self.return_stmt()
            if st is not None:
                return indent + st
            return indent + self.simple_stmt()
        if k == 4:
            return indent + self.simple_stmt()
        if k == 5:
            return indent + "if %s:\n%s%selse:\n%s" % (self.expr(), self.block(d, indent), indent,
                                                       self.block(d, indent))
        if k == 6:
            return indent + "while %s:\n%s%selse:\n%s" % (self.expr(), self.loop_body(d, indent),
                                                          indent, self.block(d, indent))
        if k == 7:
            return indent + "for %s in %s:\n%s" % (self.target(), self.expr(),
                                                   self.loop_body(d, indent))
        if k == 8:
            return indent + "def %s(%s, /, %s=%s, *%s, %s, **%s) -> %s:\n%s" % (
                r.choice(NAMES[:4]), "a", "b", self.expr(), "c", "d", "e", self.strict_expr(),
                self.func_body("func", d, indent))
        if k == 9:
            return indent + "async def %s(%s):\n%s" % (r.choice(NAMES[:4]), r.choice(NAMES[:4]),
                                                        self.func_body("async", d, indent))
        if k == 10:
            return indent + "class %s(%s):\n%s" % (r.choice(NAMES[:4]), self.expr(),
                                                   self.class_body(d, indent))
        if k == 11:
            return indent + "try:\n%s%sexcept %s as e:\n%s%sfinally:\n%s" % (
                self.block(d, indent), indent, self.expr(), self.block(d, indent), indent,
                self.block(d, indent))
        if k == 12:
            return indent + "try:\n%s%sexcept* %s:\n%s" % (
                self.block(d, indent), indent, self.expr(), self.no_control_body(d, indent))
        if k == 13:
            return indent + "with %s as %s, %s:\n%s" % (self.expr(), self.target(), self.expr(),
                                                        self.block(d, indent))
        if k == 14:
            if self.valid:
                alts = "[a, *b] | (a, b)"
            else:
                alts = "[%s, *%s] | (%s, _)" % (r.choice(NAMES[:4]), r.choice(NAMES[:4]),
                                                r.choice(NAMES[:4]))
            return indent + "match %s:\n%s    case %s if %s:\n%s%s    case {'k': %s, **r}:\n%s%s    case %s(%s=%s) as z:\n%s%s    case _:\n%s" % (
                self.expr(), indent, alts,
                self.expr(), self.block(d + 1, indent + "    "), indent, r.choice(NAMES[:4]),
                self.block(d + 1, indent + "    "), indent, r.choice(CLASS_NAMES), r.choice(NAMES),
                r.choice(NAMES[:4]), self.block(d + 1, indent + "    "), indent,
                self.block(d + 1, indent + "    "))
        if k == 15:
            if in_async or not self.valid:
                return indent + "async for %s in %s:\n%s" % (self.target(), self.expr(),
                                                             self.loop_body(d, indent))
            return indent + "for %s in %s:\n%s" % (self.target(), self.expr(),
                                                   self.loop_body(d, indent))
        if k == 16:
            if in_async or not self.valid:
                return indent + "async with %s as %s:\n%s" % (self.expr(), self.target(),
                                                              self.block(d, indent))
            return indent + "with %s as %s:\n%s" % (self.expr(), self.target(),
                                                    self.block(d, indent))
        if k == 17:
            return indent + "@%s\n%sdef %s[T, *Ts, **P](%s: T = %s) -> T:\n%s" % (
                self.expr(), indent, r.choice(NAMES[:4]), r.choice(NAMES[:4]), self.strict_expr(),
                self.func_body("func", d, indent))
        if k == 18:
            return indent + "type %s[T: %s] = %s\n" % (r.choice(NAMES[:4]), self.strict_expr(),
                                                      self.strict_expr())
        if k == 19:
            return indent + "class %s[T=%s]:\n%s" % (r.choice(NAMES[:4]), self.strict_expr(),
                                                     self.class_body(d, indent))
        if k == 20:
            return indent + "for %s in %s:\n%s%selse:\n%s" % (self.target(), self.expr(),
                                                              self.loop_body(d, indent), indent,
                                                              self.block(d, indent))
        if k == 21:
            return indent + "if %s:\n%s%selif %s:\n%s" % (self.expr(), self.block(d, indent), indent,
                                                          self.expr(), self.block(d, indent))
        if k == 22:
            return indent + "def %s():\n%s" % (r.choice(NAMES[:4]),
                                               self.func_body("func", d, indent, docstring=True))
        if k == 23:
            self.push("class")
            try:
                comp = self.comp(lambda d: "[%s for %s in %s]" % (
                    self.expr(d), r.choice(NAMES[:4]), self.expr(d)), 1)
            finally:
                self.pop()
            return indent + "class %s:\n%s    %s = %s\n%s" % (
                r.choice(NAMES[:4]), indent, r.choice(NAMES[:4]), comp,
                self.class_body(d, indent))
        if k == 24:
            # closures: nonlocal / global declarations in valid positions
            self.push("func")
            try:
                value = self.expr()
            finally:
                self.pop()
            inner = self.func_body("func", d + 1, indent + "    ")
            return indent + "def %s():\n%s    nl = %s\n%s    def g():\n%s        nonlocal nl\n%s        nl += 1\n%s%s    return g\n" % (
                r.choice(NAMES[:4]), indent, value, indent, indent, indent, inner, indent)
        if k == 25:
            self.push("func")
            try:
                value = self.expr()
            finally:
                self.pop()
            return indent + "def %s():\n%s    global gv\n%s    gv = %s\n%s" % (
                r.choice(NAMES[:4]), indent, indent, value, self.func_body("func", d, indent))
        if k == 26:
            return indent + "async def %s():\n%s" % (
                r.choice(NAMES[:4]), self.func_body("async", d, indent))
        return indent + "%s = %s\n" % (self.target(), self.expr())

    def block(self, depth, indent):
        n = self.rng.randrange(1, 4)
        return "".join(self.stmt(depth, indent + "    ") for _ in range(n))

    def program(self):
        self.scopes = ["module"]
        self.strict = self.loops = self.no_control = self.comp_depth = 0
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
    try:
        return tokenize.untokenize([(t.type, t.string) for t in toks])
    except (IndexError, ValueError, TypeError):
        # untokenize cannot cope with some indentation mutations; join instead
        return "".join(t.string if t.type in (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT)
                       else t.string + " " for t in toks)


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
    gen = Gen(rng, valid=True)
    wild = Gen(rng, valid=False)
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
        elif i % 8 == 6:
            src, label = wild.program(), "generated (unrestricted)"
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
