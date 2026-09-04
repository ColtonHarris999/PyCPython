"""Lint the shipped package for any use of eval/exec/compile (direct or
indirect) and for imports of the stdlib ast/symtable/dis modules.

Exit status 1 if a violation is found.  Run in CI on every commit.

Only tests/ and tools/ may use the forbidden names (they are the oracle).
"""
import ast
import os
import sys

FORBIDDEN_NAMES = {"eval", "exec", "compile"}
FORBIDDEN_MODULES = {"ast", "symtable", "dis", "_ast", "_symtable", "opcode", "_opcode",
                     "importlib", "runpy", "code", "codeop", "py_compile", "compileall",
                     "_compile", "_compiler", "_testinternalcapi", "ctypes", "_ctypes",
                     "subprocess", "builtins", "_imp"}
# stdlib modules the package may import (everything else is flagged so a
# reviewer sees new dependencies explicitly)
ALLOWED_MODULES = {"sys", "os", "marshal", "types", "struct", "warnings", "unicodedata",
                   "weakref", "codecs", "io", "re", "math", "string", "keyword", "enum",
                   "itertools", "collections", "functools", "operator", "errno", "locale",
                   "encodings", "_string"}
FORBIDDEN_ATTRS = {"eval", "exec", "compile"}
FORBIDDEN_STRINGS = {"eval", "exec", "compile", "__import__", "builtins"}


def check_file(path, errors):
    with open(path, "rb") as f:
        src = f.read()
    tree = ast.parse(src, path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            errors.append("%s:%d: use of forbidden name %r" % (path, node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            errors.append("%s:%d: use of forbidden attribute .%s" % (path, node.lineno, node.attr))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                names = [node.module] if node.level == 0 and node.module else []
            for n in names:
                top = n.split(".")[0]
                if top in FORBIDDEN_MODULES or (n == "builtins"):
                    errors.append("%s:%d: forbidden import %r" % (path, node.lineno, n))
                elif top not in ALLOWED_MODULES:
                    errors.append("%s:%d: import of module %r not on the allow list"
                                  % (path, node.lineno, n))
        elif isinstance(node, ast.Subscript):
            # sys.modules["builtins"], globals()["exec"], ...
            sl = node.slice
            if isinstance(sl, ast.Constant) and sl.value in FORBIDDEN_STRINGS:
                errors.append("%s:%d: subscript with %r (possible indirect access)"
                              % (path, node.lineno, sl.value))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in ("__import__", "vars", "globals", "locals"):
                    errors.append("%s:%d: call to %s() (possible indirect access)"
                                  % (path, node.lineno, func.id))
                elif func.id in ("getattr", "setattr", "delattr", "hasattr"):
                    # only allow a literal attribute name that is not forbidden
                    ok = (len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                          and isinstance(node.args[1].value, str)
                          and node.args[1].value not in FORBIDDEN_STRINGS)
                    if not ok:
                        errors.append("%s:%d: dynamic %s() (possible indirect access)"
                                      % (path, node.lineno, func.id))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "sys" and node.attr in ("modules", "_getframe", "settrace",
                                                         "setprofile"):
                errors.append("%s:%d: use of sys.%s" % (path, node.lineno, node.attr))


def main(argv):
    pkg = argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "pycpython")
    errors = []
    for root, dirs, files in os.walk(pkg):
        for fn in sorted(files):
            if fn.endswith(".py"):
                check_file(os.path.join(root, fn), errors)
    for e in errors:
        print(e)
    if errors:
        print("%d violation(s)" % len(errors))
        return 1
    print("ok: no eval/exec/compile references in", pkg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
