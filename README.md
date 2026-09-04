# pycpython — CPython 3.14's compiler, reimplemented in Python

`pycpython` is a from-scratch, importable reimplementation of the CPython
3.14 compilation pipeline:

    source -> tokenizer -> PEG parser -> AST -> AST preprocessing -> symbol
    table -> code generation -> CFG optimizer -> assembler -> code object

It produces `code` objects that are equivalent to the ones the interpreter's
builtin `compile()` produces, for the `"exec"`, `"eval"` and `"single"` modes.
The library never executes code and never uses `eval`, `exec` or `compile`
(a linter enforces this in CI); it does not use the stdlib `ast`,
`symtable` or `dis` modules either.  Code objects are constructed through
`marshal` and `types`.

```python
from pycpython.compile import compile_source

co = compile_source("def f(x):\n    return x + 1\n", "example.py", "exec")
```

## Pinned oracle

The reference implementation is **CPython 3.14.7**
(`sys.version_info == (3, 14, 7, 'final', 0)`,
`sys.implementation.cache_tag == 'cpython-314'`).  The test harness refuses
to run on any other interpreter.  Every table in `pycpython/opcodes.py` and
the AST classes in `pycpython/pyast.py` are generated from that source tree
(`tools/gen_opcodes.py`, `tools/gen_ast.py`); the parser in
`pycpython/parser/generated_parser.py` is generated from the 3.14.7
`Grammar/python.gram` by `tools/gen_parser.py`, a Python back end for
CPython's own `pegen` generator (vendored in `tools/pegen`).

## Layout

| path | contents |
|---|---|
| `pycpython/tokenizer.py` | port of `Parser/lexer` + `Parser/tokenizer` (byte offsets, f/t-string modes) |
| `pycpython/parser/` | pegen runtime (`pegen.py`), string literal decoding, generated PEG parser |
| `pycpython/pyast.py` | AST node classes generated from `Python.asdl` |
| `pycpython/future.py`, `ast_preprocess.py` | `Python/future.c`, `Python/ast_preprocess.c` |
| `pycpython/symtable.py` | `Python/symtable.c` |
| `pycpython/codegen.py` | `Python/compile.c` + `Python/codegen.c` |
| `pycpython/flowgraph.py` | `Python/flowgraph.c` (CFG construction and optimizations) |
| `pycpython/assemble.py` | `Python/assemble.c` (bytecode, location and exception tables) |
| `pycpython/marshal_writer.py` | builds the final code object with exact local kinds and constant sharing |
| `pycpython/compile.py` | the public entry points `compile_source()` and `parse_to_ast()` |
| `tools/lint_forbidden.py` | CI linter: zero `eval`/`exec`/`compile` references in the package |
| `tests/` | differential harness (see below); may use anything, never imported by the package |

## Testing

Everything is checked differentially against the oracle:

* **Tier 0** — `ast.compare(..., compare_attributes=True)` of the parsed
  module, including all positions; plus tokenizer and symbol table parity.
* **Tier 1** — structural equality of the code objects, recursively:
  bytecode, constants, names, locals, flags, argument counts, stack size,
  line table and exception table (`tests/check_codegen.py`).
* **Tier 2** — `marshal.dumps()` byte equality, measured in two identically
  initialized processes (`tests/check_tier2.py`, see `DEVIATIONS.md` for why).
* error parity corpus (`tests/errors`): same exception type and arguments.
* handwritten edge cases (`tests/cases`) in all three modes and a seeded
  fuzz/mutation harness (`tests/fuzz.py`): a scope-aware random program
  generator (most programs are valid and exercise code generation), an
  unrestricted generator and token-level mutations of corpus files (error
  parity).
* `tests/marshal_dump.py` prints a marshal stream as a flat token list so two
  Tier 2 dumps (`tests/check_tier2.py --dump-dir D`) can be `diff`ed.

Single command (this is what CI runs; add `--full` for the whole `Lib/` and
`Lib/test/` corpus):

    python tests/run_all.py --cpython /path/to/cpython-3.14.7-source

Current results are recorded in `STATUS.md`; known deviations in
`DEVIATIONS.md`.
