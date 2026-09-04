"""Single-command differential test harness.

    python tests/run_all.py [--cpython /path/to/cpython-3.14.7-source] [--full] [--quick]

Runs, in order:
  1. the forbidden-builtin linter over the shipped package
  2. Tier 0 (AST parity)      on the handwritten corpora (+ Lib corpus with --full)
  3. tokenizer + symtable parity on the same inputs
  4. Tier 1 (structural code object parity, in-process) in exec/eval/single modes
  5. Tier 2 (byte-exact marshal, two identically-initialized processes)
  6. error parity corpus (same exception type and args)
  7. fuzz / mutation parity (seeded)
Exits non-zero if any tier shows a failure that is not an accepted, documented
deviation, or if the oracle interpreter is not the pinned version.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PINNED = (3, 14, 7)


def run(cmd, **kw):
    print("$", " ".join(cmd), flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    out = p.stdout + p.stderr
    return p.returncode, out


def summary_of(out):
    for line in out.splitlines():
        if line.startswith("summary:"):
            return dict(kv.split("=") for kv in line.split()[1:])
    return None


def main(argv):
    cpython = None
    full = False
    it = iter(argv)
    for a in it:
        if a == "--cpython":
            cpython = next(it)
        elif a == "--full":
            full = True
        elif a == "--quick":
            full = False
    if sys.version_info[:3] != PINNED:
        print("ERROR: the oracle interpreter must be CPython %d.%d.%d (got %s)"
              % (PINNED + (sys.version.split()[0],)))
        return 2
    print("oracle: %s cache_tag=%s" % (sys.version.split()[0], sys.implementation.cache_tag))
    py = sys.executable
    failures = []

    rc, out = run([py, os.path.join(ROOT, "tools", "lint_forbidden.py")])
    print(out.strip())
    if rc != 0:
        failures.append("lint")

    exec_paths = [os.path.join(HERE, "cases", "exec")]
    corpus = []
    if full and cpython:
        corpus = [os.path.join(cpython, "Lib")]
    elif cpython:
        # a fixed, representative sample of the Lib corpus for quick runs
        sample = ["abc.py", "functools.py", "dataclasses.py", "typing.py", "enum.py",
                  "inspect.py", "asyncio/tasks.py", "test/test_grammar.py",
                  "test/test_patma.py", "test/test_compile.py", "test/test_syntax.py",
                  "test/test_coroutines.py", "test/test_fstring.py", "test/test_tstring.py",
                  "test/test_float.py", "test/test_exceptions.py", "test/test_scope.py",
                  "test/test_listcomps.py", "test/test_type_params.py", "test/test_dis.py"]
        corpus = [os.path.join(cpython, "Lib", s) for s in sample]

    def stage(name, cmd, ok_keys):
        rc, out = run(cmd)
        s = summary_of(out)
        print("  ", name, "->", s)
        if s is None:
            print(out[-3000:])
            failures.append(name)
            return
        bad = {k: v for k, v in s.items() if k not in ok_keys and int(v) > 0}
        if bad:
            print(out[-6000:])
            failures.append("%s %s" % (name, bad))

    check_ast = os.path.join(HERE, "check_ast.py")
    check_tok = os.path.join(HERE, "check_tokenizer.py")
    check_sym = os.path.join(HERE, "check_symtable.py")
    check_cg = os.path.join(HERE, "check_codegen.py")
    check_t2 = os.path.join(HERE, "check_tier2.py")
    fuzz = os.path.join(HERE, "fuzz.py")

    all_exec = exec_paths + corpus
    stage("tier0 (ast)", [py, check_ast] + all_exec + [os.path.join(HERE, "errors")], {"ok"})
    stage("tokenizer", [py, check_tok] + all_exec, {"ok"})
    stage("symtable", [py, check_sym] + all_exec, {"ok"})
    stage("tier1 exec", [py, check_cg] + all_exec, {"tier1", "tier2", "exc-ok"})
    stage("tier1 eval", [py, check_cg, "--mode", "eval", os.path.join(HERE, "cases", "eval")],
          {"tier1", "tier2", "exc-ok"})
    stage("tier1 single", [py, check_cg, "--mode", "single",
                           os.path.join(HERE, "cases", "single")], {"tier1", "tier2", "exc-ok"})
    stage("errors", [py, check_cg, os.path.join(HERE, "errors")], {"exc-ok", "tier2", "tier1"})
    stage("tier2 exec", [py, check_t2] + all_exec, {"tier2", "exc-ok", "tier2-roundtrip"})
    stage("tier2 eval", [py, check_t2, "--mode", "eval", os.path.join(HERE, "cases", "eval")],
          {"tier2", "exc-ok", "tier2-roundtrip"})
    stage("tier2 single", [py, check_t2, "--mode", "single",
                           os.path.join(HERE, "cases", "single")],
          {"tier2", "exc-ok", "tier2-roundtrip"})
    stage("fuzz", [py, fuzz, "--iterations", "300" if not full else "3000"] + corpus[:8],
          {"ok", "exc-ok", "tier1", "tier2"})

    if failures:
        print("FAILURES:", failures)
        return 1
    print("all stages passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
