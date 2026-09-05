# Handoff

State of the work and what a new session needs to know to continue.

## Where the work lives

All work is committed on branch `claude/cpython-3-14-compile-frontend-4o4fcz`.

**The branch has not been pushed.**  `git push` returns HTTP 403: the Claude
GitHub App does not have access to `ColtonHarris999/PyCPython`.  This is an
authorization block, not a network failure, so retrying does not help.  An
org admin can install the app at
<https://github.com/apps/claude/installations/select_target>, or the account
owner can re-link an existing installation from
<https://claude.ai/customize/connectors?auth_start=github&auth_start_force=1>.
Once access is granted the branch pushes as-is:

    git push -u origin claude/cpython-3-14-compile-frontend-4o4fcz

## Rebuilding the environment

The differential harness needs the pinned oracle interpreter and the matching
CPython source tree (for the test corpus and for consulting the C sources):

    uv python install 3.14.7
    git clone --depth 1 --branch v3.14.7 https://github.com/python/cpython.git /tmp/cpython

Everything then runs from one command (this is exactly what CI runs; add
`--full` to use the whole `Lib/` and `Lib/test/` corpus instead of a
20-file sample):

    python tests/run_all.py --cpython /tmp/cpython

The harness refuses to run on any interpreter other than 3.14.7.

## Results

See `STATUS.md` for pass rates and `DEVIATIONS.md` for the three documented
Tier 2 deviations.  Summary: Tier 0 and Tier 1 are at 100% over all 1869
files of `Lib/`; Tier 2 is byte-exact for 87.6% as dumped and 100% after one
`marshal` round trip on both sides, with zero mismatches.

## Regenerating derived sources

Three files in the package are generated from the CPython source tree and
must be regenerated together if the oracle is ever repinned:

    python tools/gen_opcodes.py /tmp/cpython   # pycpython/opcodes.py
    python tools/gen_ast.py     /tmp/cpython   # pycpython/pyast.py
    python tools/gen_parser.py  /tmp/cpython   # pycpython/parser/generated_parser.py

## Constraints that must keep holding

* The shipped package never references `eval`, `exec` or `compile`, directly
  or indirectly, and does not import the stdlib `ast`, `symtable`, `dis` or
  `opcode` modules.  `tools/lint_forbidden.py` enforces this and runs as the
  first CI step.  Test and tooling code is exempt and is never imported by
  the package.
* Code objects are built by serializing a `CodeSpec` with
  `pycpython/marshal_writer.py` and reading it back with `marshal.loads`.
  Do not switch to `types.CodeType` or `code.replace()`: neither can express
  the per-local "kinds" byte (argument kinds, hidden comprehension locals),
  and `replace()` recomputes and loses it.
* Tier 2 must be measured in two separately launched processes.  Interned
  string state carries over within one process and makes an in-process
  comparison report spurious differences (deviation D2).

## Remaining optional work

* Extend the two-process Tier 2 measurement to modes `eval` and `single`
  over a larger corpus than `tests/cases` (today those directories hold 20
  and 33 handwritten files and both are at 100%).
* Longer mutation-fuzz runs.  The generator in `tests/fuzz.py` is
  scope-aware and ~99.5% of what it emits compiles; the remaining invalid
  programs are still useful, they land in the error-parity path.
* `tests/marshal_dump.py` prints a marshal byte stream as a flat token list.
  Diffing a reference dump against ours is the fastest way to localize any
  future Tier 2 byte difference; that is how the last 39 corpus mismatches
  were traced to class `__qualname__` constant interning.
