# Status

Oracle: **CPython 3.14.7** (`sys.version_info == (3, 14, 7, 'final', 0)`,
`sys.implementation.cache_tag == 'cpython-314'`), source tree tag `v3.14.7`.
All numbers below were produced by `tests/run_all.py --full --cpython <src>`
and the individual harnesses on that interpreter.

## Corpus: every `.py` under `Lib/` and `Lib/test/` (1869 files, mode `exec`)

| tier | result |
|---|---|
| Tier 0 — `ast.compare(..., compare_attributes=True)` (positions included) via `parse_to_ast()` | **1869 / 1869 (100%)** |
| tokenizer stream parity, symbol table parity | 100% on the same files |
| Tier 1 — structural code object equality, recursive (`tests/check_codegen.py`) | **1864 / 1864 compilable files (100%)**; the other 5 files raise the same exception type and args as the oracle |
| Tier 2 — `marshal.dumps()` byte-exact, two identically initialized processes (`tests/check_tier2.py`) | **1633 / 1864 (87.6%) byte-exact as dumped**; **1864 / 1864 (100%) byte-exact after one `marshal.loads`/`dumps` round trip** on both sides; 0 mismatches |

The 231 files that are exact only after a round trip all contain at least
one single-byte `bytes` literal (`b'\n'`, `b' '`, ...); the only difference
is the `FLAG_REF` bit of that constant and the numbering of later
back-references (deviation D1 in `DEVIATIONS.md`).

The 5 files the oracle refuses to compile, where `pycpython` raises the same
`SyntaxError` with identical `args`:

* `test/tokenizedata/bad_coding.py` (unknown encoding)
* `test/tokenizedata/bad_coding2.py` (utf-8 with BOM conflict)
* `test/tokenizedata/badsyntax_3131.py` (invalid character)
* `test/tokenizedata/badsyntax_pep3120.py` (non-UTF-8 without declaration)
* `test/test_future_stmt/badsyntax_future.py` (late `__future__` import)

## Handwritten corpora

| corpus | files | Tier 0 | Tier 1 | Tier 2 (two-process) |
|---|---|---|---|---|
| `tests/cases/exec` (mode `exec`) | 26 (4 of them deliberately erroneous) | 100% | 100% | 100% (1 file `tier2-roundtrip`, D1) |
| `tests/cases/eval` (mode `eval`) | 20 | — | 100% | 100% |
| `tests/cases/single` (mode `single`) | 33 | — | 100% | 100% |
| `tests/errors` (error parity) | 257 | 100% (same exception type and `args`, including message, line, column, end position and text) | 59 compile in both, byte-exact | — |

The error corpus covers tokenizer errors (indentation, unterminated
strings/f-strings/t-strings, invalid characters, encoding declarations,
numeric literal errors), parser errors (all `invalid_*` grammar rules that
have handwritten messages, `expected ':'`, decorators, pattern matching,
walrus/positional-only/keyword-argument constraints, `ValueError` for the
identifiers `None`/`True`/`False` and for null bytes), symbol table errors
(`nonlocal`/`global` conflicts, annotated names, duplicate parameters,
comprehension/class-body restrictions, PEP 695 scopes), compiler errors
(`return`/`yield`/`await` outside function, `break` outside loop,
`except*` restrictions, `__future__` handling, too many expressions in
star-unpacking, invalid `__debug__` assignments) and decode errors
(`(unicode error)` / `(value error)` wrapping).

## Fuzz / mutation

`tests/fuzz.py` mixes three sources: a scope-aware random program generator
(≈99.5% of its programs are accepted by the oracle and go through the whole
pipeline), an unrestricted generator (exercises error parity) and token-level
mutations of corpus files.  Every program is checked in mode `exec` and
every fifth one also in mode `single`.

| run | result |
|---|---|
| `--iterations 400 --seed 7` | 480 checks, 0 mismatches (172 byte-exact, 190 Tier 1 with in-process marshal differences caused by interned-string state, 118 identical exceptions) |
| `--iterations 300` on 8 Lib files (CI quick run) | 246 checks, 0 mismatches |
| `--iterations 3000 --seed 11` on 4 Lib files | see `tests/run_all.py --full` output; 0 mismatches at the time of writing |

Bugs found by fuzzing and fixed: a crash in the parser's error-recovery
pass when the action-less alternative of `invalid_def_raw` hands a dummy
node to `function_def_decorators` (the C parser reads garbage there and
discards the result); `await -x` statement forms.

## Known failures

None on the corpora above.  See `DEVIATIONS.md` for the three documented
Tier 2 deviations (single-byte `bytes` reference flags, interpreter-state
dependent constant interning, filename identity).

## What is not implemented

* `compile()` with an AST object as input, `PyCF_ONLY_AST` / `ast.parse`
  flag plumbing beyond `parse_to_ast()`, `PyCF_ALLOW_INCOMPLETE_INPUT`
  (`codeop`) is implemented but not part of the differential tests,
  `func_type` mode.
* Nothing executes code; there is no evaluator.
