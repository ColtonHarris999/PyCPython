# Known deviations from CPython 3.14.7

This file lists every known way in which `pycpython` differs from the
reference `compile()` of the pinned oracle (CPython 3.14.7,
`sys.version_info == (3, 14, 7, 'final', 0)`, `cache_tag == 'cpython-314'`).
Anything not listed here is a bug.

## Tier 2 (byte-exact `marshal.dumps`)

### D1. Reference flags on single-byte `bytes` constants

`marshal.dumps()` marks an object with `FLAG_REF` when its reference count is
not 1.  Single-byte `bytes` objects (`b'x'`) are immortal singletons in
CPython, so a source file containing such a literal gets the flag set in the
reference output.  `pycpython` builds code objects through `marshal.loads()`,
and `marshal` always creates a fresh, non-singleton `bytes` object, so the
flag is clear.  This changes only the flag bit and the numbering of
subsequent back-references; the object graph is identical.

*Status:* structural (Tier 1) equality holds; the marshal streams become
identical after one `marshal.loads()`/`dumps()` round trip on both sides.
The Tier 2 harness reports such files as `tier2-roundtrip`.  There is no way
to obtain the interpreter's singleton from pure Python without `eval`-class
functionality, so this deviation is permanent.

### D2. Constant sharing that depends on interpreter state

CPython's `_PyCode_New()` interns identifier-like string constants and
rebuilds any `frozenset` constant whose strings get *replaced* by an
already-interned equal string.  Whether that happens depends on which
strings the running interpreter had interned before the compilation
(static strings, previously imported modules, earlier compilations).
`pycpython` reproduces the mechanism faithfully by calling `sys.intern()`
in the same order as `_PyCode_New()` would, so in a given process it makes
the same decisions the C compiler would make *at that moment*.

*Consequence for testing:* the reference and `pycpython` must be compared in
two processes with identical import histories (`tests/check_tier2.py` does
this).  Comparing them in the same process makes the second compilation see
strings interned by the first and produces spurious differences.

### D3. Filename identity

`compile()` stores the caller's filename object in `co_filename` (after
mortal interning).  `pycpython` builds its code objects through
`marshal.loads()`, so `co_filename` is an equal but distinct object unless an
equal interned string already exists.  The Tier 2 harness compares with an
extra reference held to both filename objects, which makes the reference
flag identical; user-visible behaviour is unaffected.

## Behavioural notes (not deviations)

* Warnings: `SyntaxWarning`s emitted by the compiler (e.g. `"is" with a
  literal`, `assertion is always true`, invalid escape sequences) go through
  `warnings.warn_explicit` exactly like CPython; with `-W error` they turn
  into `SyntaxError`s with the same arguments.
* Recursion limits: the reference parser and compiler fail with
  `RecursionError`/`MemoryError` at C-stack dependent depths.  `pycpython`
  raises the same exception types (`MemoryError("Parser stack overflowed -
  Python source too complex to parse")` from the parser) but the exact
  nesting depth at which they trigger differs, since it depends on the C
  stack.  The error corpus therefore only contains inputs that are well
  inside or well outside the limits.
* `compile()` modes: only the source modes `exec`, `eval` and `single` are
  implemented; `PyCF_ONLY_AST` output is available through
  `pycpython.compile.parse_to_ast()` (it applies the same AST preprocessing
  that `ast.parse()` applies) and the AST classes live in `pycpython.pyast`.
