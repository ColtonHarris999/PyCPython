"""Port of Python/compile.c (compiler state, code units, constant cache)
and Python/codegen.c (AST -> pseudo instruction sequence).

Errors are raised as exceptions (SyntaxError with the full compile.c
style args tuple) instead of the C return codes."""

import sys

from . import pyast as ast
from . import opcodes as op
from .opcodes import *  # noqa: F401,F403  (opcode names)
from . import symtable as st
from .symtable import (LOCAL, GLOBAL_EXPLICIT, GLOBAL_IMPLICIT, FREE, CELL,
                       DEF_COMP_CELL, DEF_FREE_CLASS, DEF_LOCAL, DEF_NONLOCAL,
                       DEF_IMPORT, ClassBlock, symbol_to_scope, is_function_like,
                       get_scope, get_symbol, key_of, key_plus1, mangle, maybe_mangle)
from .errors import (SourceLocation, NO_LOCATION, NEXT_LOCATION,
                     raise_syntax_error_full, emit_syntax_warning_full)
from .future import CO_FUTURE_ANNOTATIONS, get_docstring
from .instrseq import InstructionSequence, NO_LABEL
from .unparse import expr_as_unicode

# --- constants ---------------------------------------------------------

COMPILE_SCOPE_MODULE = 0
COMPILE_SCOPE_CLASS = 1
COMPILE_SCOPE_FUNCTION = 2
COMPILE_SCOPE_ASYNC_FUNCTION = 3
COMPILE_SCOPE_LAMBDA = 4
COMPILE_SCOPE_COMPREHENSION = 5
COMPILE_SCOPE_ANNOTATIONS = 6

(COMPILE_FBLOCK_WHILE_LOOP, COMPILE_FBLOCK_FOR_LOOP, COMPILE_FBLOCK_TRY_EXCEPT,
 COMPILE_FBLOCK_FINALLY_TRY, COMPILE_FBLOCK_FINALLY_END, COMPILE_FBLOCK_WITH,
 COMPILE_FBLOCK_ASYNC_WITH, COMPILE_FBLOCK_HANDLER_CLEANUP, COMPILE_FBLOCK_POP_VALUE,
 COMPILE_FBLOCK_EXCEPTION_HANDLER, COMPILE_FBLOCK_EXCEPTION_GROUP_HANDLER,
 COMPILE_FBLOCK_ASYNC_COMPREHENSION_GENERATOR, COMPILE_FBLOCK_STOP_ITERATION) = range(13)

COMPILE_OP_FAST = 0
COMPILE_OP_GLOBAL = 1
COMPILE_OP_DEREF = 2
COMPILE_OP_NAME = 3

COMP_GENEXP = 0
COMP_LISTCOMP = 1
COMP_SETCOMP = 2
COMP_DICTCOMP = 3

CO_MAXBLOCKS = 21
_PY_STACK_USE_GUIDELINE = 30

CO_OPTIMIZED = 0x0001
CO_NEWLOCALS = 0x0002
CO_VARARGS = 0x0004
CO_VARKEYWORDS = 0x0008
CO_NESTED = 0x0010
CO_GENERATOR = 0x0020
CO_COROUTINE = 0x0080
CO_ITERABLE_COROUTINE = 0x0100
CO_ASYNC_GENERATOR = 0x0200
CO_HAS_DOCSTRING = 0x4000000
CO_METHOD = 0x8000000

PyCF_MASK = (0x20000 | 0x40000 | 0x80000 | 0x100000 | 0x200000 | 0x400000
             | 0x800000 | 0x1000000)

CONSTANT_ASSERTIONERROR = 0
CONSTANT_NOTIMPLEMENTEDERROR = 1
CONSTANT_BUILTIN_TUPLE = 2
CONSTANT_BUILTIN_ALL = 3
CONSTANT_BUILTIN_ANY = 4

SPECIAL___ENTER__ = 0
SPECIAL___EXIT__ = 1
SPECIAL___AENTER__ = 2
SPECIAL___AEXIT__ = 3

FVC_NONE = 0
FVC_STR = 1
FVC_REPR = 2
FVC_ASCII = 3

_Py_ANNOTATE_FORMAT_VALUE_WITH_FAKE_GLOBALS = 2

Py_LT, Py_LE, Py_EQ, Py_NE, Py_GT, Py_GE = range(6)

compare_masks = {
    Py_LT: COMPARISON_LESS_THAN,
    Py_LE: COMPARISON_LESS_THAN | COMPARISON_EQUALS,
    Py_EQ: COMPARISON_EQUALS,
    Py_NE: COMPARISON_NOT_EQUALS,
    Py_GT: COMPARISON_GREATER_THAN,
    Py_GE: COMPARISON_GREATER_THAN | COMPARISON_EQUALS,
}

LOAD_METHOD = -1
LOAD_SUPER_METHOD = -2
LOAD_ZERO_SUPER_ATTR = -3
LOAD_ZERO_SUPER_METHOD = -4

Load, Store, Del = ast.Load, ast.Store, ast.Del


def LOC(n):
    return SourceLocation(n.lineno, n.end_lineno, n.col_offset, n.end_col_offset)


def LOCATION(lno, end_lno, col, end_col):
    return SourceLocation(lno, end_lno, col, end_col)


class CompileError(Exception):
    """Internal: never escapes; SyntaxError is raised directly."""


# --- constant keys (Objects/codeobject.c: _PyCode_ConstantKey) ------------

_code_type = type((lambda: 0).__code__)


def constant_key(o):
    t = type(o)
    if o is None or o is Ellipsis or t is int or t is str or t is _code_type:
        return o
    if t is bool or t is bytes:
        return (t, o)
    if t is float:
        if o == 0.0 and str(o).startswith('-'):
            return (t, o, None)
        return (t, o)
    if t is complex:
        real_negzero = o.real == 0.0 and str(o.real).startswith('-')
        imag_negzero = o.imag == 0.0 and str(o.imag).startswith('-')
        if real_negzero and imag_negzero:
            return (t, o, True)
        if imag_negzero:
            return (t, o, False)
        if real_negzero:
            return (t, o, None)
        return (t, o)
    if t is tuple:
        return (tuple(constant_key(item) for item in o), o)
    if t is frozenset:
        return (frozenset(constant_key(item) for item in o), o)
    if t is slice:
        return (slice(constant_key(o.start), constant_key(o.stop), constant_key(o.step)), o)
    return (id(o), o)


def const_cache_insert(const_cache, o, recursive):
    """Merge constant *o* into the cache and return its key object.  The
    returned key is either the constant itself (None, Ellipsis, int, str,
    code) or a tuple whose second item is the (merged) constant."""
    if o is None or o is Ellipsis:
        return o
    key = constant_key(o)
    t = const_cache.get(key)
    if t is not None:
        return t
    const_cache[key] = key
    if not recursive:
        return key
    if type(o) is tuple:
        items = list(o)
        changed = False
        for i, item in enumerate(o):
            u = const_cache_insert(const_cache, item, recursive)
            v = u[1] if type(u) is tuple else u
            if v is not item:
                items[i] = v
                changed = True
        if changed:
            new_o = tuple(items)
            key = (key[0], new_o)
            const_cache[key] = key
    elif type(o) is frozenset:
        if len(o) == 0:
            return key
        items = []
        for item in o:
            k = const_cache_insert(const_cache, item, recursive)
            items.append(k[1] if type(k) is tuple else k)
        new = frozenset(items)
        key = (key[0], new)
        const_cache[key] = key
    return key


def const_cache_merge_one(const_cache, obj):
    """_PyCompile_ConstCacheMergeOne."""
    key = const_cache_insert(const_cache, obj, False)
    if type(key) is tuple:
        return key[1]
    return key


def dict_add_obj(d, o):
    """_PyCompile_DictAddObj: insertion ordered index assignment."""
    v = d.get(o)
    if v is None:
        v = len(d)
        d[o] = v
    return v


def consts_dict_keys_inorder(d):
    consts = [None] * len(d)
    for k, v in d.items():
        if type(k) is tuple:
            k = k[1]
        consts[v] = k
    return consts


def clean_doc(doc):
    """_PyCompile_CleanDoc (a C implementation of inspect.cleandoc that
    keeps leading/trailing blank lines)."""
    doc = doc.expandtabs()
    n = len(doc)
    p = 0
    # skip first line
    while p < n:
        ch = doc[p]
        p += 1
        if ch == '\n':
            break
    margin = sys.maxsize
    while p < n:
        s = p
        while p < n and doc[p] == ' ':
            p += 1
        if p < n and doc[p] != '\n':
            margin = min(margin, p - s)
        while p < n:
            ch = doc[p]
            p += 1
            if ch == '\n':
                break
    if margin == sys.maxsize:
        margin = 0
    p = 0
    while p < n and doc[p] == ' ':
        p += 1
    if p == 0 and margin == 0:
        return doc
    out = []
    while p < n:
        ch = doc[p]
        p += 1
        out.append(ch)
        if ch == '\n':
            break
    while p < n:
        i = 0
        while i < margin:
            if p >= n or doc[p] != ' ':
                break
            i += 1
            p += 1
        while p < n:
            ch = doc[p]
            p += 1
            out.append(ch)
            if ch == '\n':
                break
    return "".join(out)


class CodeUnitMetadata:
    """_PyCompile_CodeUnitMetadata."""

    def __init__(self):
        self.u_name = None
        self.u_qualname = None
        self.u_consts = None
        self.u_names = None
        self.u_varnames = None
        self.u_cellvars = None
        self.u_freevars = None
        self.u_fasthidden = None
        self.u_argcount = 0
        self.u_posonlyargcount = 0
        self.u_kwonlyargcount = 0
        self.u_firstlineno = 0


class FBlockInfo:
    __slots__ = ('fb_type', 'fb_block', 'fb_loc', 'fb_exit', 'fb_datum')

    def __init__(self, t, block, loc, exit_, datum):
        self.fb_type = t
        self.fb_block = block
        self.fb_loc = loc
        self.fb_exit = exit_
        self.fb_datum = datum

    def copy(self):
        return FBlockInfo(self.fb_type, self.fb_block, self.fb_loc, self.fb_exit, self.fb_datum)


class CompilerUnit:
    def __init__(self):
        self.u_ste = None
        self.u_scope_type = 0
        self.u_private = None
        self.u_static_attributes = None
        self.u_deferred_annotations = None
        self.u_conditional_annotation_indices = None
        self.u_next_conditional_annotation_index = 0
        self.u_instr_sequence = None
        self.u_stashed_instr_sequence = None
        self.u_fblock = []
        self.u_in_inlined_comp = 0
        self.u_in_conditional_block = 0
        self.u_metadata = CodeUnitMetadata()


class InlinedComprehensionState:
    def __init__(self):
        self.pushed_locals = None
        self.temp_symbols = None
        self.fast_hidden = None
        self.cleanup = NO_LABEL


class PatternContext:
    def __init__(self):
        self.stores = None
        self.allow_irrefutable = 0
        self.fail_pop = None
        self.fail_pop_size = 0
        self.on_top = 0

    def copy(self):
        pc = PatternContext()
        pc.stores = self.stores
        pc.allow_irrefutable = self.allow_irrefutable
        pc.fail_pop = self.fail_pop
        pc.fail_pop_size = self.fail_pop_size
        pc.on_top = self.on_top
        return pc


def dictbytype(src, scope_type, flag, offset):
    dest = {}
    i = offset
    for k in sorted(src):
        vi = src[k]
        if symbol_to_scope(vi) == scope_type or vi & flag:
            dest[k] = i
            i += 1
    return dest


def infer_type_name(e):
    if isinstance(e, ast.Tuple):
        return "tuple"
    if isinstance(e, (ast.List, ast.ListComp)):
        return "list"
    if isinstance(e, (ast.Dict, ast.DictComp)):
        return "dict"
    if isinstance(e, (ast.Set, ast.SetComp)):
        return "set"
    if isinstance(e, ast.GeneratorExp):
        return "generator"
    if isinstance(e, ast.Lambda):
        return "function"
    if isinstance(e, (ast.TemplateStr, ast.Interpolation)):
        return "string.templatelib.Template"
    if isinstance(e, (ast.JoinedStr, ast.FormattedValue)):
        return "str"
    if isinstance(e, ast.Constant):
        return type(e.value).__name__
    return None


def infer_type(e):
    """Returns a type object or None (used by check_index)."""
    if isinstance(e, ast.Tuple):
        return tuple
    if isinstance(e, (ast.List, ast.ListComp)):
        return list
    if isinstance(e, (ast.Dict, ast.DictComp)):
        return dict
    if isinstance(e, (ast.Set, ast.SetComp)):
        return set
    if isinstance(e, ast.GeneratorExp):
        return type(_ for _ in ())
    if isinstance(e, ast.Lambda):
        return type(lambda: 0)
    if isinstance(e, (ast.TemplateStr, ast.Interpolation)):
        return _TemplateType
    if isinstance(e, (ast.JoinedStr, ast.FormattedValue)):
        return str
    if isinstance(e, ast.Constant):
        return type(e.value)
    return None


class _TemplateType:
    __name__ = "string.templatelib.Template"


def _tp_name(t):
    if t is _TemplateType:
        return "string.templatelib.Template"
    return t.__name__


def is_const_tuple(elts):
    for e in elts:
        if not isinstance(e, ast.Constant):
            return False
    return True


def check_is_arg(e):
    if isinstance(e, ast.Tuple):
        return not is_const_tuple(e.elts)
    if not isinstance(e, ast.Constant):
        return True
    value = e.value
    return value is None or value is False or value is True or value is Ellipsis


def is_constant_slice(s):
    return (isinstance(s, ast.Slice)
            and (s.lower is None or isinstance(s.lower, ast.Constant))
            and (s.upper is None or isinstance(s.upper, ast.Constant))
            and (s.step is None or isinstance(s.step, ast.Constant)))


def should_apply_two_element_slice_optimization(s):
    return (not is_constant_slice(s) and isinstance(s, ast.Slice) and s.step is None)


def WILDCARD_CHECK(n):
    return isinstance(n, ast.MatchAs) and n.name is None


def WILDCARD_STAR_CHECK(n):
    return isinstance(n, ast.MatchStar) and n.name is None


def MATCH_VALUE_EXPR(n):
    return isinstance(n, (ast.Constant, ast.Attribute))


_BINOP_OPARGS = {
    ast.Add: (NB_ADD, NB_INPLACE_ADD),
    ast.Sub: (NB_SUBTRACT, NB_INPLACE_SUBTRACT),
    ast.Mult: (NB_MULTIPLY, NB_INPLACE_MULTIPLY),
    ast.MatMult: (NB_MATRIX_MULTIPLY, NB_INPLACE_MATRIX_MULTIPLY),
    ast.Div: (NB_TRUE_DIVIDE, NB_INPLACE_TRUE_DIVIDE),
    ast.Mod: (NB_REMAINDER, NB_INPLACE_REMAINDER),
    ast.Pow: (NB_POWER, NB_INPLACE_POWER),
    ast.LShift: (NB_LSHIFT, NB_INPLACE_LSHIFT),
    ast.RShift: (NB_RSHIFT, NB_INPLACE_RSHIFT),
    ast.BitOr: (NB_OR, NB_INPLACE_OR),
    ast.BitXor: (NB_XOR, NB_INPLACE_XOR),
    ast.BitAnd: (NB_AND, NB_INPLACE_AND),
    ast.FloorDiv: (NB_FLOOR_DIVIDE, NB_INPLACE_FLOOR_DIVIDE),
}

_CMP_TO_PY = {ast.Eq: Py_EQ, ast.NotEq: Py_NE, ast.Lt: Py_LT, ast.LtE: Py_LE,
              ast.Gt: Py_GT, ast.GtE: Py_GE}


class Compiler:
    """struct _PyCompiler + the codegen functions."""

    def __init__(self, mod, filename, flags, optimize):
        from . import future as future_mod
        from . import ast_preprocess
        self.c_filename = filename
        self.c_const_cache = {}
        self.c_stack = []
        self.u = None
        self.c_interactive = 0
        self.c_disable_warning = 0
        self.c_save_nested_seqs = False
        self.c_future = future_mod.future_from_ast(mod, filename)
        merged = self.c_future.ff_features | flags
        self.c_future.ff_features = merged
        self.c_flags = merged
        self.c_optimize = optimize if optimize != -1 else sys.flags.optimize
        ast_preprocess.preprocess(mod, filename, self.c_optimize, merged, 0, 1)
        self.c_st = st.build(mod, filename, self.c_future)

    # -- accessors -------------------------------------------------------

    def INSTR_SEQUENCE(self):
        return self.u.u_instr_sequence

    def FUTURE_FEATURES(self):
        return self.c_future.ff_features

    def SYMTABLE_ENTRY(self):
        return self.u.u_ste

    def OPTIMIZATION_LEVEL(self):
        return self.c_optimize

    def IS_INTERACTIVE_TOP_LEVEL(self):
        return self.c_interactive and not (len(self.c_stack) > 0)

    def SCOPE_TYPE(self):
        return self.u.u_scope_type

    def QUALNAME(self):
        return self.u.u_metadata.u_qualname

    def METADATA(self):
        return self.u.u_metadata

    def is_in_inlined_comp(self):
        return self.u.u_in_inlined_comp

    # -- errors and warnings ---------------------------------------------

    def error(self, loc, msg):
        raise_syntax_error_full(msg, self.c_filename, loc)

    def warn(self, loc, msg):
        if self.c_disable_warning:
            return 0
        return emit_syntax_warning_full(msg, self.c_filename, loc)

    def mangle_name(self, name):
        return mangle(self.u.u_private, name)

    def maybe_mangle_name(self, name):
        return maybe_mangle(self.u.u_private, self.u.u_ste, name)

    # -- instruction emission ----------------------------------------------

    def new_label(self):
        return self.INSTR_SEQUENCE().new_label()

    def use_label(self, lbl):
        self.INSTR_SEQUENCE().use_label(lbl)

    def addop_i(self, loc, opcode, oparg):
        assert not op.is_assembler_opcode(opcode)
        self.INSTR_SEQUENCE().addop(opcode, oparg, loc)

    def addop(self, loc, opcode):
        assert not op.has_arg(opcode), opcode
        assert not op.is_assembler_opcode(opcode)
        self.INSTR_SEQUENCE().addop(opcode, 0, loc)

    def add_const(self, o):
        key = const_cache_insert(self.c_const_cache, o, True)
        return dict_add_obj(self.u.u_metadata.u_consts, key)

    def addop_load_const(self, loc, o):
        arg = self.add_const(o)
        self.addop_i(loc, LOAD_CONST, arg)

    def addop_o(self, loc, opcode, d, o):
        arg = dict_add_obj(d, o)
        self.addop_i(loc, opcode, arg)

    def addop_n(self, loc, opcode, o, kind):
        assert not op.has_const(opcode)
        d = getattr(self.u.u_metadata, 'u_' + kind)
        self.addop_o(loc, opcode, d, o)

    def addop_name(self, loc, opcode, o, kind='names'):
        d = getattr(self.u.u_metadata, 'u_' + kind)
        mangled = self.maybe_mangle_name(o)
        arg = dict_add_obj(d, mangled)
        if opcode == LOAD_ATTR:
            arg <<= 1
        if opcode == LOAD_METHOD:
            opcode = LOAD_ATTR
            arg <<= 1
            arg |= 1
        if opcode == LOAD_SUPER_ATTR:
            arg <<= 2
            arg |= 2
        if opcode == LOAD_SUPER_METHOD:
            opcode = LOAD_SUPER_ATTR
            arg <<= 2
            arg |= 3
        if opcode == LOAD_ZERO_SUPER_ATTR:
            opcode = LOAD_SUPER_ATTR
            arg <<= 2
        if opcode == LOAD_ZERO_SUPER_METHOD:
            opcode = LOAD_SUPER_ATTR
            arg <<= 2
            arg |= 1
        self.addop_i(loc, opcode, arg)

    def addop_j(self, loc, opcode, target):
        assert target != NO_LABEL
        assert op.has_target(opcode)
        assert not op.is_assembler_opcode(opcode)
        self.INSTR_SEQUENCE().addop(opcode, target, loc)

    def addop_compare(self, loc, cmp):
        if isinstance(cmp, ast.Is):
            self.addop_i(loc, IS_OP, 0)
            return
        if isinstance(cmp, ast.IsNot):
            self.addop_i(loc, IS_OP, 1)
            return
        if isinstance(cmp, ast.In):
            self.addop_i(loc, CONTAINS_OP, 0)
            return
        if isinstance(cmp, ast.NotIn):
            self.addop_i(loc, CONTAINS_OP, 1)
            return
        c = _CMP_TO_PY[type(cmp)]
        self.addop_i(loc, COMPARE_OP, (c << 5) | compare_masks[c])

    def addop_binary(self, loc, binop, inplace):
        oparg = _BINOP_OPARGS[type(binop)][1 if inplace else 0]
        self.addop_i(loc, BINARY_OP, oparg)

    def add_yield_from(self, loc, await_):
        send = self.new_label()
        fail = self.new_label()
        exit_ = self.new_label()
        self.use_label(send)
        self.addop_j(loc, SEND, exit_)
        self.addop_j(loc, SETUP_FINALLY, fail)
        self.addop_i(loc, YIELD_VALUE, 1)
        self.addop(NO_LOCATION, POP_BLOCK)
        self.addop_i(loc, RESUME, RESUME_AFTER_AWAIT if await_ else RESUME_AFTER_YIELD_FROM)
        self.addop_j(loc, JUMP_NO_INTERRUPT, send)
        self.use_label(fail)
        self.addop(loc, CLEANUP_THROW)
        self.use_label(exit_)
        self.addop(loc, END_SEND)

    def pop_except_and_reraise(self, loc):
        self.addop_i(loc, COPY, 3)
        self.addop(loc, POP_EXCEPT)
        self.addop_i(loc, RERAISE, 1)

    def addop_yield(self, loc):
        ste = self.SYMTABLE_ENTRY()
        if ste.generator and ste.coroutine:
            self.addop_i(loc, CALL_INTRINSIC_1, INTRINSIC_ASYNC_GEN_WRAP)
        self.addop_i(loc, YIELD_VALUE, 0)
        self.addop_i(loc, RESUME, RESUME_AFTER_YIELD)

    def call_exit_with_nones(self, loc):
        self.addop_load_const(loc, None)
        self.addop_load_const(loc, None)
        self.addop_load_const(loc, None)
        self.addop_i(loc, CALL, 3)

    # -- compile.c: scopes ---------------------------------------------------

    def maybe_add_static_attribute_to_class(self, e):
        attr_value = e.value
        if (not isinstance(attr_value, ast.Name) or not isinstance(e.ctx, Store)
                or attr_value.id != "self"):
            return
        for u in reversed(self.c_stack):
            if u.u_scope_type == COMPILE_SCOPE_CLASS:
                u.u_static_attributes.add(e.attr)
                break

    def set_qualname(self):
        u = self.u
        base = None
        stack_size = len(self.c_stack)
        assert stack_size >= 1
        if stack_size > 1:
            force_global = 0
            parent = self.c_stack[stack_size - 1]
            if parent.u_scope_type == COMPILE_SCOPE_ANNOTATIONS:
                if stack_size == 2:
                    u.u_metadata.u_qualname = u.u_metadata.u_name
                    return
                parent = self.c_stack[stack_size - 2]
            if u.u_scope_type in (COMPILE_SCOPE_FUNCTION, COMPILE_SCOPE_ASYNC_FUNCTION,
                                  COMPILE_SCOPE_CLASS):
                mangled = mangle(parent.u_private, u.u_metadata.u_name)
                scope = get_scope(parent.u_ste, mangled)
                assert scope != GLOBAL_IMPLICIT
                if scope == GLOBAL_EXPLICIT:
                    force_global = 1
            if not force_global:
                if parent.u_scope_type in (COMPILE_SCOPE_FUNCTION,
                                           COMPILE_SCOPE_ASYNC_FUNCTION,
                                           COMPILE_SCOPE_LAMBDA):
                    base = parent.u_metadata.u_qualname + ".<locals>"
                else:
                    base = parent.u_metadata.u_qualname
        if base is not None:
            name = base + "." + u.u_metadata.u_name
        else:
            name = u.u_metadata.u_name
        u.u_metadata.u_qualname = name

    def enter_scope(self, name, scope_type, key, lineno, private, umd):
        u = CompilerUnit()
        u.u_scope_type = scope_type
        if umd is not None:
            u.u_metadata = umd
        u.u_ste = self.c_st.blocks[key]
        u.u_metadata.u_name = name
        u.u_metadata.u_varnames = {n: i for i, n in enumerate(u.u_ste.varnames)}
        u.u_metadata.u_cellvars = dictbytype(u.u_ste.symbols, CELL, DEF_COMP_CELL, 0)
        if u.u_ste.needs_class_closure:
            assert u.u_scope_type == COMPILE_SCOPE_CLASS
            dict_add_obj(u.u_metadata.u_cellvars, "__class__")
        if u.u_ste.needs_classdict:
            assert u.u_scope_type == COMPILE_SCOPE_CLASS
            dict_add_obj(u.u_metadata.u_cellvars, "__classdict__")
        if u.u_ste.has_conditional_annotations:
            assert u.u_scope_type in (COMPILE_SCOPE_CLASS, COMPILE_SCOPE_MODULE)
            dict_add_obj(u.u_metadata.u_cellvars, "__conditional_annotations__")
        u.u_metadata.u_freevars = dictbytype(u.u_ste.symbols, FREE, DEF_FREE_CLASS,
                                             len(u.u_metadata.u_cellvars))
        u.u_metadata.u_fasthidden = {}
        u.u_fblock = []
        u.u_in_inlined_comp = 0
        u.u_metadata.u_firstlineno = lineno
        u.u_metadata.u_consts = {}
        u.u_metadata.u_names = {}
        u.u_deferred_annotations = None
        u.u_conditional_annotation_indices = None
        u.u_next_conditional_annotation_index = 0
        if scope_type == COMPILE_SCOPE_CLASS:
            u.u_static_attributes = set()
        else:
            u.u_static_attributes = None
        u.u_instr_sequence = InstructionSequence()
        u.u_stashed_instr_sequence = None
        if self.u is not None:
            self.c_stack.append(self.u)
            if private is None:
                private = self.u.u_private
        u.u_private = private
        self.u = u
        if scope_type != COMPILE_SCOPE_MODULE:
            self.set_qualname()

    def exit_scope(self):
        nested_seq = None
        if self.c_save_nested_seqs:
            nested_seq = self.u.u_instr_sequence
        if self.c_stack:
            self.u = self.c_stack.pop()
            if nested_seq is not None:
                self.u.u_instr_sequence.add_nested(nested_seq)
        else:
            self.u = None

    # -- fblocks ------------------------------------------------------------

    def push_fblock(self, loc, t, block_label, exit_, datum):
        if len(self.u.u_fblock) >= CO_MAXBLOCKS:
            self.error(loc, "too many statically nested blocks")
        self.u.u_fblock.append(FBlockInfo(t, block_label, loc, exit_, datum))
        if t == COMPILE_FBLOCK_FINALLY_END:
            self.c_disable_warning += 1

    def pop_fblock(self, t, block_label):
        u = self.u
        assert len(u.u_fblock) > 0
        f = u.u_fblock.pop()
        assert f.fb_type == t
        assert f.fb_block == block_label
        if t == COMPILE_FBLOCK_FINALLY_END:
            self.c_disable_warning -= 1

    def top_fblock(self):
        if not self.u.u_fblock:
            return None
        return self.u.u_fblock[-1]

    # -- names --------------------------------------------------------------

    def get_ref_type(self, name):
        if (self.u.u_scope_type == COMPILE_SCOPE_CLASS
                and name in ("__class__", "__classdict__", "__conditional_annotations__")):
            return CELL
        ste = self.u.u_ste
        scope = get_scope(ste, name)
        if scope == 0:
            raise SystemError(
                "_PyST_GetScope(name=%r) failed: unknown scope in unit %s (%r); "
                "symbols: %r; locals: %r; globals: %r"
                % (name, self.u.u_metadata.u_name, ste.id, ste.symbols,
                   self.u.u_metadata.u_varnames, self.u.u_metadata.u_names))
        return scope

    def lookup_cellvar(self, name):
        return self.u.u_metadata.u_cellvars[name]

    def lookup_arg(self, co, name):
        reftype = self.get_ref_type(name)
        if reftype == CELL:
            arg = self.u.u_metadata.u_cellvars.get(name, -1)
        else:
            arg = self.u.u_metadata.u_freevars.get(name, -1)
        if arg == -1:
            raise SystemError(
                "compiler_lookup_arg(name=%r) with reftype=%d failed in %s; "
                "freevars of code %s: %r"
                % (name, reftype, self.u.u_metadata.u_name, co.co_name, co.co_freevars))
        return arg

    def static_attributes_as_tuple(self):
        return tuple(sorted(self.u.u_static_attributes))

    def resolve_nameop(self, mangled, scope):
        d = self.u.u_metadata.u_names
        optype = COMPILE_OP_NAME
        if scope == FREE:
            d = self.u.u_metadata.u_freevars
            optype = COMPILE_OP_DEREF
        elif scope == CELL:
            d = self.u.u_metadata.u_cellvars
            optype = COMPILE_OP_DEREF
        elif scope == LOCAL:
            if is_function_like(self.u.u_ste):
                optype = COMPILE_OP_FAST
            else:
                if self.u.u_metadata.u_fasthidden.get(mangled) is True:
                    optype = COMPILE_OP_FAST
        elif scope == GLOBAL_IMPLICIT:
            if is_function_like(self.u.u_ste):
                optype = COMPILE_OP_GLOBAL
        elif scope == GLOBAL_EXPLICIT:
            optype = COMPILE_OP_GLOBAL
        arg = 0
        if optype != COMPILE_OP_FAST:
            arg = dict_add_obj(d, mangled)
        return optype, arg

    def tweak_inlined_comprehension_scopes(self, loc, entry, state):
        in_class_block = (self.u.u_ste.type == ClassBlock) and not self.u.u_in_inlined_comp
        self.u.u_in_inlined_comp += 1
        for k, v in list(entry.symbols.items()):
            symbol = v
            scope = symbol_to_scope(symbol)
            outsymbol = get_symbol(self.u.u_ste, k)
            outsc = symbol_to_scope(outsymbol)
            if ((scope != outsc and scope != FREE and not (scope == CELL and outsc == FREE))
                    or in_class_block):
                if state.temp_symbols is None:
                    state.temp_symbols = {}
                self.u.u_ste.symbols[k] = v
                state.temp_symbols[k] = outsymbol
            if (symbol & DEF_LOCAL and not (symbol & DEF_NONLOCAL)) or in_class_block:
                if not is_function_like(self.u.u_ste):
                    orig = self.u.u_metadata.u_fasthidden.get(k)
                    if orig is not True:
                        self.u.u_metadata.u_fasthidden[k] = True
                        if state.fast_hidden is None:
                            state.fast_hidden = []
                        if k not in state.fast_hidden:
                            state.fast_hidden.append(k)

    def revert_inlined_comprehension_scopes(self, loc, state):
        self.u.u_in_inlined_comp -= 1
        if state.temp_symbols:
            for k, v in state.temp_symbols.items():
                self.u.u_ste.symbols[k] = v
            state.temp_symbols = None
        if state.fast_hidden:
            for k in state.fast_hidden:
                self.u.u_metadata.u_fasthidden[k] = False
            state.fast_hidden = None

    def enter_conditional_block(self):
        self.u.u_in_conditional_block += 1

    def leave_conditional_block(self):
        assert self.u.u_in_conditional_block > 0
        self.u.u_in_conditional_block -= 1

    def add_deferred_annotation(self, s):
        if self.u.u_deferred_annotations is None:
            self.u.u_deferred_annotations = []
        if self.u.u_conditional_annotation_indices is None:
            self.u.u_conditional_annotation_indices = []
        self.u.u_deferred_annotations.append(s)
        conditional_annotation_index = None
        if self.u.u_scope_type == COMPILE_SCOPE_MODULE or self.u.u_in_conditional_block:
            index = self.u.u_next_conditional_annotation_index
            conditional_annotation_index = index
            self.u.u_next_conditional_annotation_index += 1
        else:
            index = -1
        self.u.u_conditional_annotation_indices.append(index)
        return conditional_annotation_index

    def start_annotation_setup(self):
        new_seq = InstructionSequence()
        assert self.u.u_stashed_instr_sequence is None
        self.u.u_stashed_instr_sequence = self.u.u_instr_sequence
        self.u.u_instr_sequence = new_seq

    def end_annotation_setup(self):
        assert self.u.u_stashed_instr_sequence is not None
        parent_seq = self.u.u_stashed_instr_sequence
        anno_seq = self.u.u_instr_sequence
        self.u.u_stashed_instr_sequence = None
        self.u.u_instr_sequence = parent_seq
        parent_seq.set_annotations_code(anno_seq)

    # -- code object creation -------------------------------------------

    def compute_code_flags(self):
        ste = self.u.u_ste
        flags = 0
        if is_function_like(ste):
            flags |= CO_NEWLOCALS | CO_OPTIMIZED
            if ste.nested:
                flags |= CO_NESTED
            if ste.generator and not ste.coroutine:
                flags |= CO_GENERATOR
            if ste.generator and ste.coroutine:
                flags |= CO_ASYNC_GENERATOR
            if ste.varargs:
                flags |= CO_VARARGS
            if ste.varkeywords:
                flags |= CO_VARKEYWORDS
            if ste.has_docstring:
                flags |= CO_HAS_DOCSTRING
            if ste.method:
                flags |= CO_METHOD
        if ste.coroutine and not ste.generator:
            flags |= CO_COROUTINE
        flags |= (self.c_flags & PyCF_MASK)
        return flags

    def optimize_and_assemble(self, add_none):
        from .assemble import optimize_and_assemble_code_unit
        u = self.u
        code_flags = self.compute_code_flags()
        self.add_return_at_end(add_none)
        return optimize_and_assemble_code_unit(u, self.c_const_cache, code_flags,
                                               self.c_filename)

    def add_return_at_end(self, add_none):
        if add_none:
            self.addop_load_const(NO_LOCATION, None)
        self.addop(NO_LOCATION, RETURN_VALUE)

    # -- module level ---------------------------------------------------------

    def compile_mod(self, mod):
        add_none = not isinstance(mod, ast.Expression)
        assert self.u is None
        try:
            self.codegen_mod(mod)
            co = self.optimize_and_assemble(add_none)
        finally:
            if self.u is not None:
                self.exit_scope()
        return co

    def codegen_mod(self, mod):
        self.enter_anonymous_scope(mod)
        assert self.u.u_scope_type == COMPILE_SCOPE_MODULE
        if isinstance(mod, ast.Module):
            self.codegen_module(self.start_location(mod.body), mod.body, False)
        elif isinstance(mod, ast.Interactive):
            self.c_interactive = 1
            self.codegen_module(self.start_location(mod.body), mod.body, True)
        elif isinstance(mod, ast.Expression):
            self.visit_expr(mod.body)
        else:
            raise SystemError("module kind %s should not be possible" % type(mod).__name__)

    @staticmethod
    def start_location(stmts):
        if len(stmts) > 0:
            return LOC(stmts[0])
        return SourceLocation(1, 1, 0, 0)

    def enter_anonymous_scope(self, mod):
        self.codegen_enter_scope("<module>", COMPILE_SCOPE_MODULE, key_of(mod), 1, None, None)

    def codegen_enter_scope(self, name, scope_type, key, lineno, private, umd):
        self.enter_scope(name, scope_type, key, lineno, private, umd)
        loc = LOCATION(lineno, lineno, 0, 0)
        if scope_type == COMPILE_SCOPE_MODULE:
            loc.lineno = 0
        self.addop_i(loc, RESUME, RESUME_AT_FUNC_START)
        if scope_type == COMPILE_SCOPE_MODULE:
            self.addop(loc, ANNOTATIONS_PLACEHOLDER)

    def codegen_module(self, loc, stmts, is_interactive):
        if self.SYMTABLE_ENTRY().has_conditional_annotations:
            self.addop_i(loc, BUILD_SET, 0)
            self.addop_n(loc, STORE_NAME, "__conditional_annotations__", 'names')
        self.codegen_body(loc, stmts, is_interactive)

    def codegen_body(self, loc, stmts, is_interactive):
        ste = self.SYMTABLE_ENTRY()
        if (self.FUTURE_FEATURES() & CO_FUTURE_ANNOTATIONS) and ste.annotations_used:
            self.addop(loc, SETUP_ANNOTATIONS)
        if not len(stmts):
            return
        first_instr = 0
        if not is_interactive:
            if ste.has_docstring:
                docstring = get_docstring(stmts)
                assert docstring is not None
                first_instr = 1
                assert self.OPTIMIZATION_LEVEL() < 2
                cleandoc = clean_doc(docstring)
                s0 = stmts[0]
                assert isinstance(s0, ast.Expr)
                dloc = LOC(s0.value)
                self.addop_load_const(dloc, cleandoc)
                self.nameop(NO_LOCATION, "__doc__", Store)
        for i in range(first_instr, len(stmts)):
            self.visit_stmt(stmts[i])
        if not (self.FUTURE_FEATURES() & CO_FUTURE_ANNOTATIONS):
            self.process_deferred_annotations(loc)

    # -- annotations ----------------------------------------------------------

    def setup_annotations_scope(self, loc, key, name):
        umd = CodeUnitMetadata()
        umd.u_posonlyargcount = 1
        self.codegen_enter_scope(name, COMPILE_SCOPE_ANNOTATIONS, key, loc.lineno, None, umd)
        assert not self.SYMTABLE_ENTRY().has_docstring
        self.addop_i(loc, LOAD_FAST, 0)
        self.addop_load_const(loc, _Py_ANNOTATE_FORMAT_VALUE_WITH_FAKE_GLOBALS)
        self.addop_i(loc, COMPARE_OP, (Py_GT << 5) | compare_masks[Py_GT])
        body = self.new_label()
        self.addop_j(loc, POP_JUMP_IF_FALSE, body)
        self.addop_i(loc, LOAD_COMMON_CONSTANT, CONSTANT_NOTIMPLEMENTEDERROR)
        self.addop_i(loc, RAISE_VARARGS, 1)
        self.use_label(body)

    def leave_annotations_scope(self, loc):
        try:
            self.addop(loc, RETURN_VALUE)
            # The parameter is named ".format" in the symtable; the code
            # object exposes it as "format" (C renames co_localsplusnames[0]
            # after assembling; renaming the varname first is equivalent).
            varnames = self.u.u_metadata.u_varnames
            assert next(iter(varnames)) == ".format"
            self.u.u_metadata.u_varnames = {("format" if k == ".format" else k): v
                                            for k, v in varnames.items()}
            co = self.optimize_and_assemble(1)
        except BaseException:
            self.exit_scope()
            raise
        self.exit_scope()
        self.make_closure(loc, co, 0)

    def deferred_annotations_body(self, loc, deferred_anno, conditional_annotation_indices,
                                  scope_type):
        assert len(deferred_anno) == len(conditional_annotation_indices)
        self.addop_i(loc, BUILD_MAP, 0)
        for i, s in enumerate(deferred_anno):
            mangled = self.mangle_name(s.target.id)
            idx = conditional_annotation_indices[i]
            not_set = self.new_label()
            if idx != -1:
                self.addop_load_const(LOC(s), idx)
                if scope_type == COMPILE_SCOPE_CLASS:
                    self.addop_name(LOC(s), LOAD_DEREF, "__conditional_annotations__", 'freevars')
                else:
                    self.addop_name(LOC(s), LOAD_GLOBAL, "__conditional_annotations__", 'names')
                self.addop_i(LOC(s), CONTAINS_OP, 0)
                self.addop_j(LOC(s), POP_JUMP_IF_FALSE, not_set)
            self.visit_expr(s.annotation)
            self.addop_i(LOC(s), COPY, 2)
            self.addop_load_const(LOC(s), mangled)
            self.addop(loc, STORE_SUBSCR)
            self.use_label(not_set)

    def process_deferred_annotations(self, loc):
        deferred_anno = self.u.u_deferred_annotations
        conditional_annotation_indices = self.u.u_conditional_annotation_indices
        if deferred_anno is None:
            assert conditional_annotation_indices is None
            return
        scope_type = self.SCOPE_TYPE()
        need_separate_block = scope_type == COMPILE_SCOPE_MODULE
        if need_separate_block:
            self.start_annotation_setup()
        ste = self.SYMTABLE_ENTRY()
        assert ste.annotation_block is not None
        key = ('annassign', ste.id)
        self.setup_annotations_scope(loc, key, ste.annotation_block.name)
        try:
            self.deferred_annotations_body(loc, deferred_anno, conditional_annotation_indices,
                                           scope_type)
        except BaseException:
            self.exit_scope()
            raise
        self.leave_annotations_scope(loc)
        self.nameop(loc, "__annotate_func__" if ste.type == ClassBlock else "__annotate__",
                    Store)
        if need_separate_block:
            self.end_annotation_setup()

    # -- closures / decorators / defaults ---------------------------------------

    def make_closure(self, loc, co, flags):
        if co.co_freevars:
            # PyUnstable_Code_GetFirstFree: free vars are the last slots
            for name in co.co_freevars:
                arg = self.lookup_arg(co, name)
                self.addop_i(loc, LOAD_CLOSURE, arg)
            flags |= MAKE_FUNCTION_CLOSURE
            self.addop_i(loc, BUILD_TUPLE, len(co.co_freevars))
        self.addop_load_const(loc, co)
        self.addop(loc, MAKE_FUNCTION)
        if flags & MAKE_FUNCTION_CLOSURE:
            self.addop_i(loc, SET_FUNCTION_ATTRIBUTE, MAKE_FUNCTION_CLOSURE)
        if flags & MAKE_FUNCTION_ANNOTATIONS:
            self.addop_i(loc, SET_FUNCTION_ATTRIBUTE, MAKE_FUNCTION_ANNOTATIONS)
        if flags & MAKE_FUNCTION_ANNOTATE:
            self.addop_i(loc, SET_FUNCTION_ATTRIBUTE, MAKE_FUNCTION_ANNOTATE)
        if flags & MAKE_FUNCTION_KWDEFAULTS:
            self.addop_i(loc, SET_FUNCTION_ATTRIBUTE, MAKE_FUNCTION_KWDEFAULTS)
        if flags & MAKE_FUNCTION_DEFAULTS:
            self.addop_i(loc, SET_FUNCTION_ATTRIBUTE, MAKE_FUNCTION_DEFAULTS)

    def decorators(self, decos):
        for d in decos:
            self.visit_expr(d)

    def apply_decorators(self, decos):
        for d in reversed(decos):
            self.addop_i(LOC(d), CALL, 0)

    def kwonlydefaults(self, loc, kwonlyargs, kw_defaults):
        default_count = 0
        for i, arg in enumerate(kwonlyargs):
            default_ = kw_defaults[i]
            if default_ is not None:
                default_count += 1
                mangled = self.maybe_mangle_name(arg.arg)
                self.addop_load_const(loc, mangled)
                self.visit_expr(default_)
        if default_count:
            self.addop_i(loc, BUILD_MAP, default_count)
            return 1
        return 0

    def visit_annexpr(self, annotation):
        loc = LOC(annotation)
        self.addop_load_const(loc, expr_as_unicode(annotation))

    def argannotation(self, id_, annotation, annotations_len, loc):
        if annotation is None:
            return annotations_len
        mangled = self.maybe_mangle_name(id_)
        self.addop_load_const(loc, mangled)
        if self.FUTURE_FEATURES() & CO_FUTURE_ANNOTATIONS:
            self.visit_annexpr(annotation)
        else:
            if isinstance(annotation, ast.Starred):
                self.visit_expr(annotation.value)
                self.addop_i(loc, UNPACK_SEQUENCE, 1)
            else:
                self.visit_expr(annotation)
        return annotations_len + 1

    def argannotations(self, args, annotations_len, loc):
        for arg in args:
            annotations_len = self.argannotation(arg.arg, arg.annotation, annotations_len, loc)
        return annotations_len

    def annotations_in_scope(self, loc, args, returns):
        n = 0
        n = self.argannotations(args.args, n, loc)
        n = self.argannotations(args.posonlyargs, n, loc)
        if args.vararg is not None and args.vararg.annotation is not None:
            n = self.argannotation(args.vararg.arg, args.vararg.annotation, n, loc)
        n = self.argannotations(args.kwonlyargs, n, loc)
        if args.kwarg is not None and args.kwarg.annotation is not None:
            n = self.argannotation(args.kwarg.arg, args.kwarg.annotation, n, loc)
        n = self.argannotation("return", returns, n, loc)
        return n

    def function_annotations(self, loc, args, returns):
        ste = self.c_st.blocks.get(key_of(args))
        assert ste is not None
        if ste.annotations_used:
            self.setup_annotations_scope(loc, key_of(args), ste.name)
            try:
                annotations_len = self.annotations_in_scope(loc, args, returns)
            except BaseException:
                self.exit_scope()
                raise
            self.addop_i(loc, BUILD_MAP, annotations_len)
            self.leave_annotations_scope(loc)
            return MAKE_FUNCTION_ANNOTATE
        return 0

    def defaults(self, args, loc):
        for d in args.defaults:
            self.visit_expr(d)
        self.addop_i(loc, BUILD_TUPLE, len(args.defaults))

    def default_arguments(self, loc, args):
        funcflags = 0
        if args.defaults and len(args.defaults) > 0:
            self.defaults(args, loc)
            funcflags |= MAKE_FUNCTION_DEFAULTS
        if args.kwonlyargs is not None:
            res = self.kwonlydefaults(loc, args.kwonlyargs, args.kw_defaults)
            if res > 0:
                funcflags |= MAKE_FUNCTION_KWDEFAULTS
        return funcflags

    def wrap_in_stopiteration_handler(self):
        handler = self.new_label()
        self.INSTR_SEQUENCE().insert_instruction(0, SETUP_CLEANUP, handler, NO_LOCATION)
        self.addop_load_const(NO_LOCATION, None)
        self.addop(NO_LOCATION, RETURN_VALUE)
        self.use_label(handler)
        self.addop_i(NO_LOCATION, CALL_INTRINSIC_1, INTRINSIC_STOPITERATION_ERROR)
        self.addop_i(NO_LOCATION, RERAISE, 1)

    def type_param_bound_or_default(self, e, name, key, allow_starred):
        self.addop_load_const(LOC(e), (1,))
        self.setup_annotations_scope(LOC(e), key, name)
        try:
            if allow_starred and isinstance(e, ast.Starred):
                self.visit_expr(e.value)
                self.addop_i(LOC(e), UNPACK_SEQUENCE, 1)
            else:
                self.visit_expr(e)
            self.addop(LOC(e), RETURN_VALUE)
            co = self.optimize_and_assemble(1)
        finally:
            self.exit_scope()
        self.make_closure(LOC(e), co, MAKE_FUNCTION_DEFAULTS)

    def type_params(self, type_params):
        if not type_params:
            return
        n = len(type_params)
        seen_default = False
        for typeparam in type_params:
            loc = LOC(typeparam)
            if isinstance(typeparam, ast.TypeVar):
                self.addop_load_const(loc, typeparam.name)
                if typeparam.bound is not None:
                    bound = typeparam.bound
                    self.type_param_bound_or_default(bound, typeparam.name, key_of(typeparam),
                                                     False)
                    intrinsic = (INTRINSIC_TYPEVAR_WITH_CONSTRAINTS if isinstance(bound, ast.Tuple)
                                 else INTRINSIC_TYPEVAR_WITH_BOUND)
                    self.addop_i(loc, CALL_INTRINSIC_2, intrinsic)
                else:
                    self.addop_i(loc, CALL_INTRINSIC_1, INTRINSIC_TYPEVAR)
                if typeparam.default_value is not None:
                    seen_default = True
                    self.type_param_bound_or_default(typeparam.default_value, typeparam.name,
                                                     key_plus1(typeparam), False)
                    self.addop_i(loc, CALL_INTRINSIC_2, INTRINSIC_SET_TYPEPARAM_DEFAULT)
                elif seen_default:
                    self.error(loc, "non-default type parameter '%s' follows default type parameter"
                               % typeparam.name)
                self.addop_i(loc, COPY, 1)
                self.nameop(loc, typeparam.name, Store)
            elif isinstance(typeparam, ast.TypeVarTuple):
                self.addop_load_const(loc, typeparam.name)
                self.addop_i(loc, CALL_INTRINSIC_1, INTRINSIC_TYPEVARTUPLE)
                if typeparam.default_value is not None:
                    self.type_param_bound_or_default(typeparam.default_value, typeparam.name,
                                                     key_of(typeparam), True)
                    self.addop_i(loc, CALL_INTRINSIC_2, INTRINSIC_SET_TYPEPARAM_DEFAULT)
                    seen_default = True
                elif seen_default:
                    self.error(loc, "non-default type parameter '%s' follows default type parameter"
                               % typeparam.name)
                self.addop_i(loc, COPY, 1)
                self.nameop(loc, typeparam.name, Store)
            elif isinstance(typeparam, ast.ParamSpec):
                self.addop_load_const(loc, typeparam.name)
                self.addop_i(loc, CALL_INTRINSIC_1, INTRINSIC_PARAMSPEC)
                if typeparam.default_value is not None:
                    self.type_param_bound_or_default(typeparam.default_value, typeparam.name,
                                                     key_of(typeparam), False)
                    self.addop_i(loc, CALL_INTRINSIC_2, INTRINSIC_SET_TYPEPARAM_DEFAULT)
                    seen_default = True
                elif seen_default:
                    self.error(loc, "non-default type parameter '%s' follows default type parameter"
                               % typeparam.name)
                self.addop_i(loc, COPY, 1)
                self.nameop(loc, typeparam.name, Store)
        self.addop_i(LOC(type_params[0]), BUILD_TUPLE, n)

    # -- functions / classes / type aliases --------------------------------------

    def function_body(self, s, is_async, funcflags, firstlineno):
        args = s.args
        name = s.name
        body = s.body
        scope_type = COMPILE_SCOPE_ASYNC_FUNCTION if is_async else COMPILE_SCOPE_FUNCTION
        umd = CodeUnitMetadata()
        umd.u_argcount = len(args.args)
        umd.u_posonlyargcount = len(args.posonlyargs)
        umd.u_kwonlyargcount = len(args.kwonlyargs)
        self.codegen_enter_scope(name, scope_type, key_of(s), firstlineno, None, umd)
        try:
            ste = self.SYMTABLE_ENTRY()
            first_instr = 0
            if ste.has_docstring:
                docstring = get_docstring(body)
                assert docstring is not None
                first_instr = 1
                docstring = clean_doc(docstring)
                self.add_const(docstring)
            start = self.new_label()
            self.use_label(start)
            add_stopiteration_handler = ste.coroutine or ste.generator
            if add_stopiteration_handler:
                self.push_fblock(NO_LOCATION, COMPILE_FBLOCK_STOP_ITERATION, start, NO_LABEL, None)
            for i in range(first_instr, len(body)):
                self.visit_stmt(body[i])
            if add_stopiteration_handler:
                self.wrap_in_stopiteration_handler()
                self.pop_fblock(COMPILE_FBLOCK_STOP_ITERATION, start)
            co = self.optimize_and_assemble(1)
        finally:
            self.exit_scope()
        self.make_closure(LOC(s), co, funcflags)

    def function(self, s, is_async):
        args = s.args
        returns = s.returns
        decos = s.decorator_list
        name = s.name
        type_params = s.type_params
        self.decorators(decos)
        firstlineno = s.lineno
        if len(decos):
            firstlineno = decos[0].lineno
        loc = LOC(s)
        is_generic = len(type_params) > 0
        funcflags = self.default_arguments(loc, args)
        num_typeparam_args = 0
        if is_generic:
            if funcflags & MAKE_FUNCTION_DEFAULTS:
                num_typeparam_args += 1
            if funcflags & MAKE_FUNCTION_KWDEFAULTS:
                num_typeparam_args += 1
            if num_typeparam_args == 2:
                self.addop_i(loc, SWAP, 2)
            type_params_name = "<generic parameters of %s>" % name
            umd = CodeUnitMetadata()
            umd.u_argcount = num_typeparam_args
            self.codegen_enter_scope(type_params_name, COMPILE_SCOPE_ANNOTATIONS,
                                     key_of(type_params), firstlineno, None, umd)
            try:
                self.type_params(type_params)
                for i in range(num_typeparam_args):
                    self.addop_i(loc, LOAD_FAST, i)
            except BaseException:
                self.exit_scope()
                raise
        try:
            annotations_flag = self.function_annotations(loc, args, returns)
        except BaseException:
            if is_generic:
                self.exit_scope()
            raise
        funcflags |= annotations_flag
        try:
            self.function_body(s, is_async, funcflags, firstlineno)
        except BaseException:
            if is_generic:
                self.exit_scope()
            raise
        if is_generic:
            try:
                self.addop_i(loc, SWAP, 2)
                self.addop_i(loc, CALL_INTRINSIC_2, INTRINSIC_SET_FUNCTION_TYPE_PARAMS)
                co = self.optimize_and_assemble(0)
            finally:
                self.exit_scope()
            self.make_closure(loc, co, 0)
            if num_typeparam_args > 0:
                self.addop_i(loc, SWAP, num_typeparam_args + 1)
                self.addop_i(loc, CALL, num_typeparam_args - 1)
            else:
                self.addop(loc, PUSH_NULL)
                self.addop_i(loc, CALL, 0)
        self.apply_decorators(decos)
        self.nameop(loc, name, Store)

    def set_type_params_in_class(self, loc):
        self.nameop(loc, ".type_params", Load)
        self.nameop(loc, "__type_params__", Store)

    def class_body(self, s, firstlineno):
        self.codegen_enter_scope(s.name, COMPILE_SCOPE_CLASS, key_of(s), firstlineno, s.name, None)
        try:
            loc = LOCATION(firstlineno, firstlineno, 0, 0)
            self.nameop(loc, "__name__", Load)
            self.nameop(loc, "__module__", Store)
            self.addop_load_const(loc, self.QUALNAME())
            self.nameop(loc, "__qualname__", Store)
            self.addop_load_const(loc, self.METADATA().u_firstlineno)
            self.nameop(loc, "__firstlineno__", Store)
            type_params = s.type_params
            if len(type_params) > 0:
                self.set_type_params_in_class(loc)
            if self.SYMTABLE_ENTRY().needs_classdict:
                self.addop(loc, LOAD_LOCALS)
                self.addop_n(loc, STORE_DEREF, "__classdict__", 'cellvars')
            if self.SYMTABLE_ENTRY().has_conditional_annotations:
                self.addop_i(loc, BUILD_SET, 0)
                self.addop_n(loc, STORE_DEREF, "__conditional_annotations__", 'cellvars')
            self.codegen_body(loc, s.body, False)
            static_attributes = self.static_attributes_as_tuple()
            self.addop_load_const(NO_LOCATION, static_attributes)
            self.nameop(NO_LOCATION, "__static_attributes__", Store)
            if self.SYMTABLE_ENTRY().needs_classdict:
                i = self.lookup_cellvar("__classdict__")
                self.addop_i(NO_LOCATION, LOAD_CLOSURE, i)
                self.nameop(NO_LOCATION, "__classdictcell__", Store)
            if self.SYMTABLE_ENTRY().needs_class_closure:
                i = self.lookup_cellvar("__class__")
                self.addop_i(NO_LOCATION, LOAD_CLOSURE, i)
                self.addop_i(NO_LOCATION, COPY, 1)
                self.nameop(NO_LOCATION, "__classcell__", Store)
            else:
                self.addop_load_const(NO_LOCATION, None)
            self.addop(NO_LOCATION, RETURN_VALUE)
            co = self.optimize_and_assemble(1)
        finally:
            self.exit_scope()
        loc = LOC(s)
        self.addop(loc, LOAD_BUILD_CLASS)
        self.addop(loc, PUSH_NULL)
        self.make_closure(loc, co, 0)
        self.addop_load_const(loc, s.name)

    def class_(self, s):
        decos = s.decorator_list
        self.decorators(decos)
        firstlineno = s.lineno
        if len(decos):
            firstlineno = decos[0].lineno
        loc = LOC(s)
        type_params = s.type_params
        is_generic = len(type_params) > 0
        if is_generic:
            type_params_name = "<generic parameters of %s>" % s.name
            self.codegen_enter_scope(type_params_name, COMPILE_SCOPE_ANNOTATIONS,
                                     key_of(type_params), firstlineno, s.name, None)
            try:
                self.type_params(type_params)
                self.nameop(loc, ".type_params", Store)
            except BaseException:
                self.exit_scope()
                raise
        try:
            self.class_body(s, firstlineno)
        except BaseException:
            if is_generic:
                self.exit_scope()
            raise
        if is_generic:
            try:
                self.nameop(loc, ".type_params", Load)
                self.addop_i(loc, CALL_INTRINSIC_1, INTRINSIC_SUBSCRIPT_GENERIC)
                self.nameop(loc, ".generic_base", Store)
                self.call_helper_impl(loc, 2, s.bases, ".generic_base", s.keywords)
                co = self.optimize_and_assemble(0)
            finally:
                self.exit_scope()
            self.make_closure(loc, co, 0)
            self.addop(loc, PUSH_NULL)
            self.addop_i(loc, CALL, 0)
        else:
            self.call_helper(loc, 2, s.bases, s.keywords)
        self.apply_decorators(decos)
        self.nameop(loc, s.name, Store)

    def typealias_body(self, s):
        loc = LOC(s)
        name = s.name.id
        self.addop_load_const(loc, (1,))
        self.setup_annotations_scope(LOC(s), key_of(s), name)
        try:
            assert not self.SYMTABLE_ENTRY().has_docstring
            self.visit_expr(s.value)
            self.addop(loc, RETURN_VALUE)
            co = self.optimize_and_assemble(0)
        finally:
            self.exit_scope()
        self.make_closure(loc, co, MAKE_FUNCTION_DEFAULTS)
        self.addop_i(loc, BUILD_TUPLE, 3)
        self.addop_i(loc, CALL_INTRINSIC_1, INTRINSIC_TYPEALIAS)

    def typealias(self, s):
        loc = LOC(s)
        type_params = s.type_params
        is_generic = len(type_params) > 0
        name = s.name.id
        if is_generic:
            type_params_name = "<generic parameters of %s>" % name
            self.codegen_enter_scope(type_params_name, COMPILE_SCOPE_ANNOTATIONS,
                                     key_of(type_params), loc.lineno, None, None)
            try:
                self.addop_load_const(loc, name)
                self.type_params(type_params)
            except BaseException:
                self.exit_scope()
                raise
        else:
            self.addop_load_const(loc, name)
            self.addop_load_const(loc, None)
        try:
            self.typealias_body(s)
        except BaseException:
            if is_generic:
                self.exit_scope()
            raise
        if is_generic:
            try:
                co = self.optimize_and_assemble(0)
            finally:
                self.exit_scope()
            self.make_closure(loc, co, 0)
            self.addop(loc, PUSH_NULL)
            self.addop_i(loc, CALL, 0)
        self.nameop(loc, name, Store)

    # -- fblock unwinding -----------------------------------------------------

    def unwind_fblock(self, ploc, info, preserve_tos):
        """Returns the (possibly updated) location."""
        t = info.fb_type
        if t in (COMPILE_FBLOCK_WHILE_LOOP, COMPILE_FBLOCK_EXCEPTION_HANDLER,
                 COMPILE_FBLOCK_EXCEPTION_GROUP_HANDLER,
                 COMPILE_FBLOCK_ASYNC_COMPREHENSION_GENERATOR,
                 COMPILE_FBLOCK_STOP_ITERATION):
            return ploc
        if t == COMPILE_FBLOCK_FOR_LOOP:
            if preserve_tos:
                self.addop_i(ploc, SWAP, 2)
            self.addop(ploc, POP_TOP)
            return ploc
        if t == COMPILE_FBLOCK_TRY_EXCEPT:
            self.addop(ploc, POP_BLOCK)
            return ploc
        if t == COMPILE_FBLOCK_FINALLY_TRY:
            self.addop(ploc, POP_BLOCK)
            if preserve_tos:
                self.push_fblock(ploc, COMPILE_FBLOCK_POP_VALUE, NO_LABEL, NO_LABEL, None)
            for stmt in info.fb_datum:
                self.visit_stmt(stmt)
            if preserve_tos:
                self.pop_fblock(COMPILE_FBLOCK_POP_VALUE, NO_LABEL)
            return NO_LOCATION
        if t == COMPILE_FBLOCK_FINALLY_END:
            if preserve_tos:
                self.addop_i(ploc, SWAP, 2)
            self.addop(ploc, POP_TOP)
            if preserve_tos:
                self.addop_i(ploc, SWAP, 2)
            self.addop(ploc, POP_BLOCK)
            self.addop(ploc, POP_EXCEPT)
            return ploc
        if t in (COMPILE_FBLOCK_WITH, COMPILE_FBLOCK_ASYNC_WITH):
            ploc = info.fb_loc
            self.addop(ploc, POP_BLOCK)
            if preserve_tos:
                self.addop_i(ploc, SWAP, 3)
                self.addop_i(ploc, SWAP, 2)
            self.call_exit_with_nones(ploc)
            if t == COMPILE_FBLOCK_ASYNC_WITH:
                self.addop_i(ploc, GET_AWAITABLE, 2)
                self.addop_load_const(ploc, None)
                self.add_yield_from(ploc, 1)
            self.addop(ploc, POP_TOP)
            return NO_LOCATION
        if t == COMPILE_FBLOCK_HANDLER_CLEANUP:
            if info.fb_datum is not None:
                self.addop(ploc, POP_BLOCK)
            if preserve_tos:
                self.addop_i(ploc, SWAP, 2)
            self.addop(ploc, POP_BLOCK)
            self.addop(ploc, POP_EXCEPT)
            if info.fb_datum is not None:
                self.addop_load_const(ploc, None)
                self.nameop(ploc, info.fb_datum, Store)
                self.nameop(ploc, info.fb_datum, Del)
            return ploc
        if t == COMPILE_FBLOCK_POP_VALUE:
            if preserve_tos:
                self.addop_i(ploc, SWAP, 2)
            self.addop(ploc, POP_TOP)
            return ploc
        raise SystemError("unreachable")

    def unwind_fblock_stack(self, ploc, preserve_tos, want_loop):
        """Returns (loc, loop) where loop is the loop fblock found (or None)."""
        top = self.top_fblock()
        if top is None:
            return ploc, None
        if top.fb_type == COMPILE_FBLOCK_EXCEPTION_GROUP_HANDLER:
            self.error(ploc, "'break', 'continue' and 'return' cannot appear in an except* block")
        if want_loop and top.fb_type in (COMPILE_FBLOCK_WHILE_LOOP, COMPILE_FBLOCK_FOR_LOOP):
            return ploc, top
        copy = top.copy()
        self.pop_fblock(top.fb_type, top.fb_block)
        ploc = self.unwind_fblock(ploc, copy, preserve_tos)
        ploc, loop = self.unwind_fblock_stack(ploc, preserve_tos, want_loop)
        self.push_fblock(copy.fb_loc, copy.fb_type, copy.fb_block, copy.fb_exit, copy.fb_datum)
        return ploc, loop

    # -- comparisons / jumps ---------------------------------------------------

    def check_compare(self, e):
        left = check_is_arg(e.left)
        left_expr = e.left
        n = len(e.ops)
        for i in range(n):
            cmp = e.ops[i]
            right_expr = e.comparators[i]
            right = check_is_arg(right_expr)
            if isinstance(cmp, (ast.Is, ast.IsNot)):
                if not right or not left:
                    if isinstance(cmp, ast.Is):
                        msg = "\"is\" with '%.200s' literal. Did you mean \"==\"?"
                    else:
                        msg = "\"is not\" with '%.200s' literal. Did you mean \"!=\"?"
                    literal = left_expr if not left else right_expr
                    return self.warn(LOC(e), msg % infer_type_name(literal))
            left = right
            left_expr = right_expr
        return 0

    def jump_if(self, loc, e, next_, cond):
        if isinstance(e, ast.UnaryOp):
            if isinstance(e.op, ast.Not):
                return self.jump_if(loc, e.operand, next_, not cond)
        elif isinstance(e, ast.BoolOp):
            s = e.values
            n = len(s) - 1
            assert n >= 0
            cond2 = isinstance(e.op, ast.Or)
            next2 = next_
            if (not cond2) != (not cond):
                next2 = self.new_label()
            for i in range(n):
                self.jump_if(loc, s[i], next2, cond2)
            self.jump_if(loc, s[n], next_, cond)
            if next2 != next_:
                self.use_label(next2)
            return
        elif isinstance(e, ast.IfExp):
            end = self.new_label()
            next2 = self.new_label()
            self.jump_if(loc, e.test, next2, 0)
            self.jump_if(loc, e.body, next_, cond)
            self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, end)
            self.use_label(next2)
            self.jump_if(loc, e.orelse, next_, cond)
            self.use_label(end)
            return
        elif isinstance(e, ast.Compare):
            n = len(e.ops) - 1
            if n > 0:
                self.check_compare(e)
                cleanup = self.new_label()
                self.visit_expr(e.left)
                for i in range(n):
                    self.visit_expr(e.comparators[i])
                    self.addop_i(LOC(e), SWAP, 2)
                    self.addop_i(LOC(e), COPY, 2)
                    self.addop_compare(LOC(e), e.ops[i])
                    self.addop(LOC(e), TO_BOOL)
                    self.addop_j(LOC(e), POP_JUMP_IF_FALSE, cleanup)
                self.visit_expr(e.comparators[n])
                self.addop_compare(LOC(e), e.ops[n])
                self.addop(LOC(e), TO_BOOL)
                self.addop_j(LOC(e), POP_JUMP_IF_TRUE if cond else POP_JUMP_IF_FALSE, next_)
                end = self.new_label()
                self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, end)
                self.use_label(cleanup)
                self.addop(LOC(e), POP_TOP)
                if not cond:
                    self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, next_)
                self.use_label(end)
                return
        self.visit_expr(e)
        self.addop(LOC(e), TO_BOOL)
        self.addop_j(LOC(e), POP_JUMP_IF_TRUE if cond else POP_JUMP_IF_FALSE, next_)

    def ifexp(self, e):
        end = self.new_label()
        next_ = self.new_label()
        self.jump_if(LOC(e), e.test, next_, 0)
        self.visit_expr(e.body)
        self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, end)
        self.use_label(next_)
        self.visit_expr(e.orelse)
        self.use_label(end)

    def lambda_(self, e):
        args = e.args
        loc = LOC(e)
        funcflags = self.default_arguments(loc, args)
        umd = CodeUnitMetadata()
        umd.u_argcount = len(args.args)
        umd.u_posonlyargcount = len(args.posonlyargs)
        umd.u_kwonlyargcount = len(args.kwonlyargs)
        self.codegen_enter_scope("<lambda>", COMPILE_SCOPE_LAMBDA, key_of(e), e.lineno, None, umd)
        try:
            assert not self.SYMTABLE_ENTRY().has_docstring
            self.visit_expr(e.body)
            if self.SYMTABLE_ENTRY().generator:
                co = self.optimize_and_assemble(0)
            else:
                self.addop(LOC(e.body), RETURN_VALUE)
                co = self.optimize_and_assemble(1)
        finally:
            self.exit_scope()
        self.make_closure(loc, co, funcflags)

    # -- statements -------------------------------------------------------------

    def if_(self, s):
        end = self.new_label()
        if len(s.orelse):
            next_ = self.new_label()
        else:
            next_ = end
        self.jump_if(LOC(s), s.test, next_, 0)
        self.visit_stmts(s.body)
        if len(s.orelse):
            self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, end)
            self.use_label(next_)
            self.visit_stmts(s.orelse)
        self.use_label(end)

    def for_(self, s):
        loc = LOC(s)
        start = self.new_label()
        body = self.new_label()
        cleanup = self.new_label()
        end = self.new_label()
        self.push_fblock(loc, COMPILE_FBLOCK_FOR_LOOP, start, end, None)
        self.visit_expr(s.iter)
        loc = LOC(s.iter)
        self.addop(loc, GET_ITER)
        self.use_label(start)
        self.addop_j(loc, FOR_ITER, cleanup)
        self.addop(LOC(s.target), NOP)
        self.use_label(body)
        self.visit_expr(s.target)
        self.visit_stmts(s.body)
        self.addop_j(NO_LOCATION, JUMP, start)
        self.use_label(cleanup)
        self.addop(NO_LOCATION, END_FOR)
        self.addop(NO_LOCATION, POP_ITER)
        self.pop_fblock(COMPILE_FBLOCK_FOR_LOOP, start)
        self.visit_stmts(s.orelse)
        self.use_label(end)

    def async_for(self, s):
        loc = LOC(s)
        start = self.new_label()
        send = self.new_label()
        except_ = self.new_label()
        end = self.new_label()
        self.visit_expr(s.iter)
        self.addop(LOC(s.iter), GET_AITER)
        self.use_label(start)
        self.push_fblock(loc, COMPILE_FBLOCK_FOR_LOOP, start, end, None)
        self.addop_j(loc, SETUP_FINALLY, except_)
        self.addop(loc, GET_ANEXT)
        self.addop_load_const(loc, None)
        self.use_label(send)
        self.add_yield_from(loc, 1)
        self.addop(loc, POP_BLOCK)
        self.addop(loc, NOT_TAKEN)
        self.visit_expr(s.target)
        self.visit_stmts(s.body)
        self.addop_j(NO_LOCATION, JUMP, start)
        self.pop_fblock(COMPILE_FBLOCK_FOR_LOOP, start)
        self.use_label(except_)
        loc = LOC(s.iter)
        self.addop_j(loc, END_ASYNC_FOR, send)
        self.visit_stmts(s.orelse)
        self.use_label(end)

    def while_(self, s):
        loop = self.new_label()
        end = self.new_label()
        anchor = self.new_label()
        self.use_label(loop)
        self.push_fblock(LOC(s), COMPILE_FBLOCK_WHILE_LOOP, loop, end, None)
        self.jump_if(LOC(s), s.test, anchor, 0)
        self.visit_stmts(s.body)
        self.addop_j(NO_LOCATION, JUMP, loop)
        self.pop_fblock(COMPILE_FBLOCK_WHILE_LOOP, loop)
        self.use_label(anchor)
        if s.orelse:
            self.visit_stmts(s.orelse)
        self.use_label(end)

    def return_(self, s):
        loc = LOC(s)
        preserve_tos = (s.value is not None) and not isinstance(s.value, ast.Constant)
        ste = self.SYMTABLE_ENTRY()
        if not is_function_like(ste):
            self.error(loc, "'return' outside function")
        if s.value is not None and ste.coroutine and ste.generator:
            self.error(loc, "'return' with value in async generator")
        if preserve_tos:
            self.visit_expr(s.value)
        else:
            if s.value is not None:
                loc = LOC(s.value)
                self.addop(loc, NOP)
        if s.value is None or s.value.lineno != s.lineno:
            loc = LOC(s)
            self.addop(loc, NOP)
        loc, _ = self.unwind_fblock_stack(loc, preserve_tos, False)
        if s.value is None:
            self.addop_load_const(loc, None)
        elif not preserve_tos:
            self.addop_load_const(loc, s.value.value)
        self.addop(loc, RETURN_VALUE)

    def break_(self, loc):
        origin_loc = loc
        self.addop(loc, NOP)
        loc, loop = self.unwind_fblock_stack(loc, 0, True)
        if loop is None:
            self.error(origin_loc, "'break' outside loop")
        loc = self.unwind_fblock(loc, loop, 0)
        self.addop_j(loc, JUMP, loop.fb_exit)

    def continue_(self, loc):
        origin_loc = loc
        self.addop(loc, NOP)
        loc, loop = self.unwind_fblock_stack(loc, 0, True)
        if loop is None:
            self.error(origin_loc, "'continue' not properly in loop")
        self.addop_j(loc, JUMP, loop.fb_block)

    def try_finally(self, s):
        loc = LOC(s)
        body = self.new_label()
        end = self.new_label()
        exit_ = self.new_label()
        cleanup = self.new_label()
        self.addop_j(loc, SETUP_FINALLY, end)
        self.use_label(body)
        self.push_fblock(loc, COMPILE_FBLOCK_FINALLY_TRY, body, end, s.finalbody)
        if s.handlers and len(s.handlers):
            self.try_except(s)
        else:
            self.visit_stmts(s.body)
        self.addop(NO_LOCATION, POP_BLOCK)
        self.pop_fblock(COMPILE_FBLOCK_FINALLY_TRY, body)
        self.visit_stmts(s.finalbody)
        self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, exit_)
        self.use_label(end)
        loc = NO_LOCATION
        self.addop_j(loc, SETUP_CLEANUP, cleanup)
        self.addop(loc, PUSH_EXC_INFO)
        self.push_fblock(loc, COMPILE_FBLOCK_FINALLY_END, end, NO_LABEL, None)
        self.visit_stmts(s.finalbody)
        self.pop_fblock(COMPILE_FBLOCK_FINALLY_END, end)
        loc = NO_LOCATION
        self.addop_i(loc, RERAISE, 0)
        self.use_label(cleanup)
        self.pop_except_and_reraise(loc)
        self.use_label(exit_)

    def try_star_finally(self, s):
        loc = LOC(s)
        body = self.new_label()
        end = self.new_label()
        exit_ = self.new_label()
        cleanup = self.new_label()
        self.addop_j(loc, SETUP_FINALLY, end)
        self.use_label(body)
        self.push_fblock(loc, COMPILE_FBLOCK_FINALLY_TRY, body, end, s.finalbody)
        if s.handlers and len(s.handlers):
            self.try_star_except(s)
        else:
            self.visit_stmts(s.body)
        self.addop(NO_LOCATION, POP_BLOCK)
        self.pop_fblock(COMPILE_FBLOCK_FINALLY_TRY, body)
        self.visit_stmts(s.finalbody)
        self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, exit_)
        self.use_label(end)
        loc = NO_LOCATION
        self.addop_j(loc, SETUP_CLEANUP, cleanup)
        self.addop(loc, PUSH_EXC_INFO)
        self.push_fblock(loc, COMPILE_FBLOCK_FINALLY_END, end, NO_LABEL, None)
        self.visit_stmts(s.finalbody)
        self.pop_fblock(COMPILE_FBLOCK_FINALLY_END, end)
        loc = NO_LOCATION
        self.addop_i(loc, RERAISE, 0)
        self.use_label(cleanup)
        self.pop_except_and_reraise(loc)
        self.use_label(exit_)

    def try_except(self, s):
        loc = LOC(s)
        body = self.new_label()
        except_ = self.new_label()
        end = self.new_label()
        cleanup = self.new_label()
        self.addop_j(loc, SETUP_FINALLY, except_)
        self.use_label(body)
        self.push_fblock(loc, COMPILE_FBLOCK_TRY_EXCEPT, body, NO_LABEL, None)
        self.visit_stmts(s.body)
        self.pop_fblock(COMPILE_FBLOCK_TRY_EXCEPT, body)
        self.addop(NO_LOCATION, POP_BLOCK)
        if s.orelse and len(s.orelse):
            self.visit_stmts(s.orelse)
        self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, end)
        n = len(s.handlers)
        self.use_label(except_)
        self.addop_j(NO_LOCATION, SETUP_CLEANUP, cleanup)
        self.addop(NO_LOCATION, PUSH_EXC_INFO)
        self.push_fblock(loc, COMPILE_FBLOCK_EXCEPTION_HANDLER, NO_LABEL, NO_LABEL, None)
        for i in range(n):
            handler = s.handlers[i]
            hloc = LOC(handler)
            if handler.type is None and i < n - 1:
                self.error(hloc, "default 'except:' must be last")
            next_except = self.new_label()
            except_ = next_except
            if handler.type is not None:
                self.visit_expr(handler.type)
                self.addop(hloc, CHECK_EXC_MATCH)
                self.addop_j(hloc, POP_JUMP_IF_FALSE, except_)
            if handler.name is not None:
                cleanup_end = self.new_label()
                cleanup_body = self.new_label()
                self.nameop(hloc, handler.name, Store)
                self.addop_j(hloc, SETUP_CLEANUP, cleanup_end)
                self.use_label(cleanup_body)
                self.push_fblock(hloc, COMPILE_FBLOCK_HANDLER_CLEANUP, cleanup_body, NO_LABEL,
                                 handler.name)
                self.visit_stmts(handler.body)
                self.pop_fblock(COMPILE_FBLOCK_HANDLER_CLEANUP, cleanup_body)
                self.addop(NO_LOCATION, POP_BLOCK)
                self.addop(NO_LOCATION, POP_BLOCK)
                self.addop(NO_LOCATION, POP_EXCEPT)
                self.addop_load_const(NO_LOCATION, None)
                self.nameop(NO_LOCATION, handler.name, Store)
                self.nameop(NO_LOCATION, handler.name, Del)
                self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, end)
                self.use_label(cleanup_end)
                self.addop_load_const(NO_LOCATION, None)
                self.nameop(NO_LOCATION, handler.name, Store)
                self.nameop(NO_LOCATION, handler.name, Del)
                self.addop_i(NO_LOCATION, RERAISE, 1)
            else:
                cleanup_body = self.new_label()
                self.addop(hloc, POP_TOP)
                self.use_label(cleanup_body)
                self.push_fblock(hloc, COMPILE_FBLOCK_HANDLER_CLEANUP, cleanup_body, NO_LABEL, None)
                self.visit_stmts(handler.body)
                self.pop_fblock(COMPILE_FBLOCK_HANDLER_CLEANUP, cleanup_body)
                self.addop(NO_LOCATION, POP_BLOCK)
                self.addop(NO_LOCATION, POP_EXCEPT)
                self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, end)
            self.use_label(except_)
        self.pop_fblock(COMPILE_FBLOCK_EXCEPTION_HANDLER, NO_LABEL)
        self.addop_i(NO_LOCATION, RERAISE, 0)
        self.use_label(cleanup)
        self.pop_except_and_reraise(NO_LOCATION)
        self.use_label(end)

    def try_star_except(self, s):
        loc = LOC(s)
        body = self.new_label()
        except_ = self.new_label()
        orelse = self.new_label()
        end = self.new_label()
        cleanup = self.new_label()
        reraise_star = self.new_label()
        self.addop_j(loc, SETUP_FINALLY, except_)
        self.use_label(body)
        self.push_fblock(loc, COMPILE_FBLOCK_TRY_EXCEPT, body, NO_LABEL, None)
        self.visit_stmts(s.body)
        self.pop_fblock(COMPILE_FBLOCK_TRY_EXCEPT, body)
        self.addop(NO_LOCATION, POP_BLOCK)
        self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, orelse)
        n = len(s.handlers)
        self.use_label(except_)
        self.addop_j(NO_LOCATION, SETUP_CLEANUP, cleanup)
        self.addop(NO_LOCATION, PUSH_EXC_INFO)
        self.push_fblock(loc, COMPILE_FBLOCK_EXCEPTION_GROUP_HANDLER, NO_LABEL, NO_LABEL,
                         "except handler")
        for i in range(n):
            handler = s.handlers[i]
            hloc = LOC(handler)
            next_except = self.new_label()
            except_ = next_except
            except_with_error = self.new_label()
            no_match = self.new_label()
            if i == 0:
                self.addop_i(hloc, BUILD_LIST, 0)
                self.addop_i(hloc, COPY, 2)
            if handler.type is not None:
                self.visit_expr(handler.type)
                self.addop(hloc, CHECK_EG_MATCH)
                self.addop_i(hloc, COPY, 1)
                self.addop_j(hloc, POP_JUMP_IF_NONE, no_match)
            cleanup_end = self.new_label()
            cleanup_body = self.new_label()
            if handler.name is not None:
                self.nameop(hloc, handler.name, Store)
            else:
                self.addop(hloc, POP_TOP)
            self.addop_j(hloc, SETUP_CLEANUP, cleanup_end)
            self.use_label(cleanup_body)
            self.push_fblock(hloc, COMPILE_FBLOCK_HANDLER_CLEANUP, cleanup_body, NO_LABEL,
                             handler.name)
            self.visit_stmts(handler.body)
            self.pop_fblock(COMPILE_FBLOCK_HANDLER_CLEANUP, cleanup_body)
            self.addop(NO_LOCATION, POP_BLOCK)
            if handler.name is not None:
                self.addop_load_const(NO_LOCATION, None)
                self.nameop(NO_LOCATION, handler.name, Store)
                self.nameop(NO_LOCATION, handler.name, Del)
            self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, except_)
            self.use_label(cleanup_end)
            if handler.name is not None:
                self.addop_load_const(NO_LOCATION, None)
                self.nameop(NO_LOCATION, handler.name, Store)
                self.nameop(NO_LOCATION, handler.name, Del)
            self.addop_i(NO_LOCATION, LIST_APPEND, 3)
            self.addop(NO_LOCATION, POP_TOP)
            self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, except_with_error)
            self.use_label(except_)
            self.addop(NO_LOCATION, NOP)
            self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, except_with_error)
            self.use_label(no_match)
            self.addop(hloc, POP_TOP)
            self.use_label(except_with_error)
            if i == n - 1:
                self.addop_i(NO_LOCATION, LIST_APPEND, 1)
                self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, reraise_star)
        self.pop_fblock(COMPILE_FBLOCK_EXCEPTION_GROUP_HANDLER, NO_LABEL)
        reraise = self.new_label()
        self.use_label(reraise_star)
        self.addop_i(NO_LOCATION, CALL_INTRINSIC_2, INTRINSIC_PREP_RERAISE_STAR)
        self.addop_i(NO_LOCATION, COPY, 1)
        self.addop_j(NO_LOCATION, POP_JUMP_IF_NOT_NONE, reraise)
        self.addop(NO_LOCATION, POP_TOP)
        self.addop(NO_LOCATION, POP_BLOCK)
        self.addop(NO_LOCATION, POP_EXCEPT)
        self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, end)
        self.use_label(reraise)
        self.addop(NO_LOCATION, POP_BLOCK)
        self.addop_i(NO_LOCATION, SWAP, 2)
        self.addop(NO_LOCATION, POP_EXCEPT)
        self.addop_i(NO_LOCATION, RERAISE, 0)
        self.use_label(cleanup)
        self.pop_except_and_reraise(NO_LOCATION)
        self.use_label(orelse)
        self.visit_stmts(s.orelse)
        self.use_label(end)

    def try_(self, s):
        if s.finalbody and len(s.finalbody):
            self.try_finally(s)
        else:
            self.try_except(s)

    def try_star(self, s):
        if s.finalbody and len(s.finalbody):
            self.try_star_finally(s)
        else:
            self.try_star_except(s)

    def import_as(self, loc, name, asname):
        dot = name.find('.')
        if dot != -1:
            while True:
                pos = dot + 1
                dot = name.find('.', pos)
                attr = name[pos:dot if dot != -1 else len(name)]
                self.addop_n(loc, IMPORT_FROM, attr, 'names')
                if dot == -1:
                    break
                self.addop_i(loc, SWAP, 2)
                self.addop(loc, POP_TOP)
            self.nameop(loc, asname, Store)
            self.addop(loc, POP_TOP)
            return
        self.nameop(loc, asname, Store)

    def import_(self, s):
        loc = LOC(s)
        for alias in s.names:
            self.addop_load_const(loc, 0)
            self.addop_load_const(loc, None)
            self.addop_name(loc, IMPORT_NAME, alias.name, 'names')
            if alias.asname is not None:
                self.import_as(loc, alias.name, alias.asname)
            else:
                tmp = alias.name
                dot = alias.name.find('.')
                if dot != -1:
                    tmp = alias.name[:dot]
                self.nameop(loc, tmp, Store)

    def from_import(self, s):
        n = len(s.names)
        self.addop_load_const(LOC(s), s.level)
        names = tuple(alias.name for alias in s.names)
        self.addop_load_const(LOC(s), names)
        if s.module is not None:
            self.addop_name(LOC(s), IMPORT_NAME, s.module, 'names')
        else:
            self.addop_name(LOC(s), IMPORT_NAME, "", 'names')
        for i in range(n):
            alias = s.names[i]
            if i == 0 and alias.name[0] == '*':
                assert n == 1
                self.addop_i(LOC(s), CALL_INTRINSIC_1, INTRINSIC_IMPORT_STAR)
                self.addop(NO_LOCATION, POP_TOP)
                return
            self.addop_name(LOC(s), IMPORT_FROM, alias.name, 'names')
            store_name = alias.name
            if alias.asname is not None:
                store_name = alias.asname
            self.nameop(LOC(s), store_name, Store)
        self.addop(LOC(s), POP_TOP)

    def assert_(self, s):
        if ((isinstance(s.test, ast.Tuple) and len(s.test.elts) > 0)
                or (isinstance(s.test, ast.Constant) and isinstance(s.test.value, tuple)
                    and len(s.test.value) > 0)):
            self.warn(LOC(s), "assertion is always true, perhaps remove parentheses?")
        if self.OPTIMIZATION_LEVEL():
            return
        end = self.new_label()
        self.jump_if(LOC(s), s.test, end, 1)
        self.addop_i(LOC(s), LOAD_COMMON_CONSTANT, CONSTANT_ASSERTIONERROR)
        if s.msg is not None:
            self.visit_expr(s.msg)
            self.addop_i(LOC(s), CALL, 0)
        self.addop_i(LOC(s.test), RAISE_VARARGS, 1)
        self.use_label(end)

    def stmt_expr(self, loc, value):
        if self.IS_INTERACTIVE_TOP_LEVEL():
            self.visit_expr(value)
            self.addop_i(loc, CALL_INTRINSIC_1, INTRINSIC_PRINT)
            self.addop(NO_LOCATION, POP_TOP)
            return
        if isinstance(value, ast.Constant):
            self.addop(loc, NOP)
            return
        self.visit_expr(value)
        self.addop(NO_LOCATION, POP_TOP)

    def _cond_block(self, func, s):
        self.enter_conditional_block()
        try:
            func(s)
        finally:
            self.leave_conditional_block()

    def visit_stmts(self, seq):
        for s in seq:
            self.visit_stmt(s)

    def visit_stmt(self, s):
        t = type(s)
        if t is ast.FunctionDef:
            self.function(s, 0)
        elif t is ast.ClassDef:
            self.class_(s)
        elif t is ast.TypeAlias:
            self.typealias(s)
        elif t is ast.Return:
            self.return_(s)
        elif t is ast.Delete:
            for e in s.targets:
                self.visit_expr(e)
        elif t is ast.Assign:
            n = len(s.targets)
            self.visit_expr(s.value)
            for i in range(n):
                if i < n - 1:
                    self.addop_i(LOC(s), COPY, 1)
                self.visit_expr(s.targets[i])
        elif t is ast.AugAssign:
            self.augassign(s)
        elif t is ast.AnnAssign:
            self.annassign(s)
        elif t is ast.For:
            self._cond_block(self.for_, s)
        elif t is ast.While:
            self._cond_block(self.while_, s)
        elif t is ast.If:
            self._cond_block(self.if_, s)
        elif t is ast.Match:
            self._cond_block(self.match, s)
        elif t is ast.Raise:
            n = 0
            if s.exc is not None:
                self.visit_expr(s.exc)
                n += 1
                if s.cause is not None:
                    self.visit_expr(s.cause)
                    n += 1
            self.addop_i(LOC(s), RAISE_VARARGS, n)
        elif t is ast.Try:
            self._cond_block(self.try_, s)
        elif t is ast.TryStar:
            self._cond_block(self.try_star, s)
        elif t is ast.Assert:
            self.assert_(s)
        elif t is ast.Import:
            self.import_(s)
        elif t is ast.ImportFrom:
            self.from_import(s)
        elif t is ast.Global or t is ast.Nonlocal:
            pass
        elif t is ast.Expr:
            self.stmt_expr(LOC(s), s.value)
        elif t is ast.Pass:
            self.addop(LOC(s), NOP)
        elif t is ast.Break:
            self.break_(LOC(s))
        elif t is ast.Continue:
            self.continue_(LOC(s))
        elif t is ast.With:
            self._cond_block(self.with_, s)
        elif t is ast.AsyncFunctionDef:
            self.function(s, 1)
        elif t is ast.AsyncWith:
            self._cond_block(self.async_with, s)
        elif t is ast.AsyncFor:
            self._cond_block(self.async_for, s)
        else:
            raise SystemError("unknown statement kind %s" % t.__name__)

    # -- names ------------------------------------------------------------------

    def load_classdict_freevar(self, loc):
        self.addop_n(loc, LOAD_DEREF, "__classdict__", 'freevars')

    def nameop(self, loc, name, ctx):
        assert name not in ("None", "True", "False")
        mangled = self.maybe_mangle_name(name)
        ste = self.SYMTABLE_ENTRY()
        scope = get_scope(ste, mangled)
        optype, arg = self.resolve_nameop(mangled, scope)
        if isinstance(ctx, ast.AST):
            ctx = type(ctx)
        op_ = 0
        if optype == COMPILE_OP_DEREF:
            if ctx is Load:
                if ste.type == ClassBlock and not self.is_in_inlined_comp():
                    op_ = LOAD_FROM_DICT_OR_DEREF
                    self.INSTR_SEQUENCE().addop(LOAD_LOCALS, 0, loc)
                elif ste.can_see_class_scope:
                    op_ = LOAD_FROM_DICT_OR_DEREF
                    self.load_classdict_freevar(loc)
                else:
                    op_ = LOAD_DEREF
            elif ctx is Store:
                op_ = STORE_DEREF
            elif ctx is Del:
                op_ = DELETE_DEREF
        elif optype == COMPILE_OP_FAST:
            if ctx is Load:
                op_ = LOAD_FAST
            elif ctx is Store:
                op_ = STORE_FAST
            elif ctx is Del:
                op_ = DELETE_FAST
            self.addop_n(loc, op_, mangled, 'varnames')
            return
        elif optype == COMPILE_OP_GLOBAL:
            if ctx is Load:
                if ste.can_see_class_scope and scope == GLOBAL_IMPLICIT:
                    op_ = LOAD_FROM_DICT_OR_GLOBALS
                    self.load_classdict_freevar(loc)
                else:
                    op_ = LOAD_GLOBAL
            elif ctx is Store:
                op_ = STORE_GLOBAL
            elif ctx is Del:
                op_ = DELETE_GLOBAL
        elif optype == COMPILE_OP_NAME:
            if ctx is Load:
                op_ = (LOAD_GLOBAL if (ste.type == ClassBlock and self.is_in_inlined_comp())
                       else LOAD_NAME)
            elif ctx is Store:
                op_ = STORE_NAME
            elif ctx is Del:
                op_ = DELETE_NAME
        assert op_
        if op_ == LOAD_GLOBAL:
            arg <<= 1
        self.addop_i(loc, op_, arg)

    def boolop(self, e):
        loc = LOC(e)
        jumpi = JUMP_IF_FALSE if isinstance(e.op, ast.And) else JUMP_IF_TRUE
        end = self.new_label()
        s = e.values
        n = len(s) - 1
        assert n >= 0
        for i in range(n):
            self.visit_expr(s[i])
            self.addop_j(loc, jumpi, end)
            self.addop(loc, POP_TOP)
        self.visit_expr(s[n])
        self.use_label(end)

    def starunpack_helper_impl(self, loc, elts, injected_arg, pushed, build, add, extend, tuple_):
        n = len(elts)
        big = n + pushed + (1 if injected_arg is not None else 0) > _PY_STACK_USE_GUIDELINE
        seen_star = 0
        for elt in elts:
            if isinstance(elt, ast.Starred):
                seen_star = 1
                break
        if not seen_star and not big:
            for elt in elts:
                self.visit_expr(elt)
            if injected_arg is not None:
                self.nameop(loc, injected_arg, Load)
                n += 1
            if tuple_:
                self.addop_i(loc, BUILD_TUPLE, n + pushed)
            else:
                self.addop_i(loc, build, n + pushed)
            return
        sequence_built = 0
        if big:
            self.addop_i(loc, build, pushed)
            sequence_built = 1
        for i, elt in enumerate(elts):
            if isinstance(elt, ast.Starred):
                if sequence_built == 0:
                    self.addop_i(loc, build, i + pushed)
                    sequence_built = 1
                self.visit_expr(elt.value)
                self.addop_i(loc, extend, 1)
            else:
                self.visit_expr(elt)
                if sequence_built:
                    self.addop_i(loc, add, 1)
        assert sequence_built
        if injected_arg is not None:
            self.nameop(loc, injected_arg, Load)
            self.addop_i(loc, add, 1)
        if tuple_:
            self.addop_i(loc, CALL_INTRINSIC_1, INTRINSIC_LIST_TO_TUPLE)

    def starunpack_helper(self, loc, elts, pushed, build, add, extend, tuple_):
        self.starunpack_helper_impl(loc, elts, None, pushed, build, add, extend, tuple_)

    def unpack_helper(self, loc, elts):
        n = len(elts)
        seen_star = 0
        for i, elt in enumerate(elts):
            if isinstance(elt, ast.Starred) and not seen_star:
                if i >= (1 << 8) or (n - i - 1) >= ((1 << 31) - 1) >> 8:
                    self.error(loc, "too many expressions in star-unpacking assignment")
                self.addop_i(loc, UNPACK_EX, (i + ((n - i - 1) << 8)))
                seen_star = 1
            elif isinstance(elt, ast.Starred):
                self.error(loc, "multiple starred expressions in assignment")
        if not seen_star:
            self.addop_i(loc, UNPACK_SEQUENCE, n)

    def assignment_helper(self, loc, elts):
        self.unpack_helper(loc, elts)
        for elt in elts:
            self.visit_expr(elt if not isinstance(elt, ast.Starred) else elt.value)

    def list_(self, e):
        loc = LOC(e)
        elts = e.elts
        if isinstance(e.ctx, ast.Store):
            self.assignment_helper(loc, elts)
        elif isinstance(e.ctx, ast.Load):
            self.starunpack_helper(loc, elts, 0, BUILD_LIST, LIST_APPEND, LIST_EXTEND, 0)
        else:
            for elt in elts:
                self.visit_expr(elt)

    def tuple_(self, e):
        loc = LOC(e)
        elts = e.elts
        if isinstance(e.ctx, ast.Store):
            self.assignment_helper(loc, elts)
        elif isinstance(e.ctx, ast.Load):
            self.starunpack_helper(loc, elts, 0, BUILD_LIST, LIST_APPEND, LIST_EXTEND, 1)
        else:
            for elt in elts:
                self.visit_expr(elt)

    def set_(self, e):
        loc = LOC(e)
        self.starunpack_helper(loc, e.elts, 0, BUILD_SET, SET_ADD, SET_UPDATE, 0)

    def subdict(self, e, begin, end):
        n = end - begin
        big = n * 2 > _PY_STACK_USE_GUIDELINE
        loc = LOC(e)
        if big:
            self.addop_i(loc, BUILD_MAP, 0)
        for i in range(begin, end):
            self.visit_expr(e.keys[i])
            self.visit_expr(e.values[i])
            if big:
                self.addop_i(loc, MAP_ADD, 1)
        if not big:
            self.addop_i(loc, BUILD_MAP, n)

    def dict_(self, e):
        loc = LOC(e)
        n = len(e.values)
        have_dict = 0
        elements = 0
        for i in range(n):
            is_unpacking = e.keys[i] is None
            if is_unpacking:
                if elements:
                    self.subdict(e, i - elements, i)
                    if have_dict:
                        self.addop_i(loc, DICT_UPDATE, 1)
                    have_dict = 1
                    elements = 0
                if have_dict == 0:
                    self.addop_i(loc, BUILD_MAP, 0)
                    have_dict = 1
                self.visit_expr(e.values[i])
                self.addop_i(loc, DICT_UPDATE, 1)
            else:
                if elements * 2 > _PY_STACK_USE_GUIDELINE:
                    self.subdict(e, i - elements, i + 1)
                    if have_dict:
                        self.addop_i(loc, DICT_UPDATE, 1)
                    have_dict = 1
                    elements = 0
                else:
                    elements += 1
        if elements:
            self.subdict(e, n - elements, n)
            if have_dict:
                self.addop_i(loc, DICT_UPDATE, 1)
            have_dict = 1
        if not have_dict:
            self.addop_i(loc, BUILD_MAP, 0)

    def compare(self, e):
        loc = LOC(e)
        self.check_compare(e)
        self.visit_expr(e.left)
        assert len(e.ops) > 0
        n = len(e.ops) - 1
        if n == 0:
            self.visit_expr(e.comparators[0])
            self.addop_compare(loc, e.ops[0])
        else:
            cleanup = self.new_label()
            for i in range(n):
                self.visit_expr(e.comparators[i])
                self.addop_i(loc, SWAP, 2)
                self.addop_i(loc, COPY, 2)
                self.addop_compare(loc, e.ops[i])
                self.addop_i(loc, COPY, 1)
                self.addop(loc, TO_BOOL)
                self.addop_j(loc, POP_JUMP_IF_FALSE, cleanup)
                self.addop(loc, POP_TOP)
            self.visit_expr(e.comparators[n])
            self.addop_compare(loc, e.ops[n])
            end = self.new_label()
            self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, end)
            self.use_label(cleanup)
            self.addop_i(loc, SWAP, 2)
            self.addop(loc, POP_TOP)
            self.use_label(end)

    def check_caller(self, e):
        if isinstance(e, (ast.Constant, ast.Tuple, ast.List, ast.ListComp, ast.Dict,
                          ast.DictComp, ast.Set, ast.SetComp, ast.GeneratorExp,
                          ast.JoinedStr, ast.TemplateStr, ast.FormattedValue,
                          ast.Interpolation)):
            return self.warn(LOC(e), "'%.200s' object is not callable; "
                                     "perhaps you missed a comma?" % infer_type_name(e))
        return 0

    def check_subscripter(self, e):
        if isinstance(e, ast.Constant):
            v = e.value
            if not (v is None or v is Ellipsis or isinstance(v, (int, float, complex,
                                                                  set, frozenset))):
                return 0
        elif not isinstance(e, (ast.Set, ast.SetComp, ast.GeneratorExp, ast.TemplateStr,
                                ast.Interpolation, ast.Lambda)):
            return 0
        return self.warn(LOC(e), "'%.200s' object is not subscriptable; "
                                 "perhaps you missed a comma?" % infer_type_name(e))

    def check_index(self, e, s):
        index_type = infer_type(s)
        if (index_type is None or (isinstance(index_type, type) and issubclass(index_type, int))
                or index_type is slice):
            return 0
        if isinstance(e, ast.Constant):
            v = e.value
            if not isinstance(v, (str, bytes, tuple)):
                return 0
        elif not isinstance(e, (ast.Tuple, ast.List, ast.ListComp, ast.JoinedStr,
                                ast.FormattedValue)):
            return 0
        return self.warn(LOC(e), "%.200s indices must be integers or slices, not %.200s; "
                                 "perhaps you missed a comma?"
                         % (infer_type_name(e), _tp_name(index_type)))

    def is_import_originated(self, e):
        if not isinstance(e, ast.Name):
            return 0
        flags = get_symbol(self.c_st.top, e.id)
        return flags & DEF_IMPORT

    def can_optimize_super_call(self, attr):
        e = attr.value
        if (not isinstance(e, ast.Call) or not isinstance(e.func, ast.Name)
                or e.func.id != "super" or attr.attr == "__class__"
                or len(e.keywords) != 0):
            return 0
        num_args = len(e.args)
        super_name = e.func.id
        scope = get_scope(self.SYMTABLE_ENTRY(), super_name)
        if scope != GLOBAL_IMPLICIT:
            return 0
        scope = get_scope(self.c_st.top, super_name)
        if scope != 0:
            return 0
        if num_args == 2:
            for elt in e.args:
                if isinstance(elt, ast.Starred):
                    return 0
            return 1
        if num_args != 0:
            return 0
        if self.METADATA().u_argcount == 0 and self.METADATA().u_posonlyargcount == 0:
            return 0
        if self.get_ref_type("__class__") == FREE:
            return 1
        return 0

    def load_args_for_super(self, e):
        loc = LOC(e)
        super_name = e.func.id
        self.nameop(LOC(e.func), super_name, Load)
        if len(e.args) == 2:
            self.visit_expr(e.args[0])
            self.visit_expr(e.args[1])
            return
        name = "__class__"
        assert self.get_ref_type(name) == FREE
        self.nameop(loc, name, Load)
        varnames = self.METADATA().u_varnames
        if not varnames:
            raise SystemError("no first argument")
        key = next(iter(varnames))
        self.nameop(loc, key, Load)

    @staticmethod
    def update_start_location_to_match_attr(loc, attr):
        if loc.lineno != attr.end_lineno:
            loc = SourceLocation(loc.lineno, loc.end_lineno, loc.col_offset, loc.end_col_offset)
            loc.lineno = attr.end_lineno
            n = len(attr.attr)
            if n <= attr.end_col_offset:
                loc.col_offset = attr.end_col_offset - n
            else:
                loc.col_offset = -1
                loc.end_col_offset = -1
            loc.end_lineno = max(loc.lineno, loc.end_lineno)
            if loc.lineno == loc.end_lineno:
                loc.end_col_offset = max(loc.col_offset, loc.end_col_offset)
        return loc

    def maybe_optimize_function_call(self, e, end):
        args = e.args
        kwds = e.keywords
        func = e.func
        if not (isinstance(func, ast.Name) and len(args) == 1 and len(kwds) == 0
                and isinstance(args[0], ast.GeneratorExp)):
            return 0
        generator_exp = args[0]
        generator_entry = self.c_st.blocks[key_of(generator_exp)]
        if generator_entry.coroutine:
            return 0
        loc = LOC(func)
        optimized = 0
        skip_optimization = self.new_label()
        const_oparg = -1
        initial_res = None
        continue_jump_opcode = -1
        if func.id == "all":
            const_oparg = CONSTANT_BUILTIN_ALL
            initial_res = True
            continue_jump_opcode = POP_JUMP_IF_TRUE
        elif func.id == "any":
            const_oparg = CONSTANT_BUILTIN_ANY
            initial_res = False
            continue_jump_opcode = POP_JUMP_IF_FALSE
        elif func.id == "tuple":
            const_oparg = CONSTANT_BUILTIN_TUPLE
        if const_oparg != -1:
            self.addop_i(loc, COPY, 1)
            self.addop_i(loc, LOAD_COMMON_CONSTANT, const_oparg)
            self.addop_compare(loc, ast.Is_singleton)
            self.addop_j(loc, POP_JUMP_IF_FALSE, skip_optimization)
            self.addop(loc, POP_TOP)
            if const_oparg == CONSTANT_BUILTIN_TUPLE:
                self.addop_i(loc, BUILD_LIST, 0)
            self.visit_expr(generator_exp)
            loop = self.new_label()
            cleanup = self.new_label()
            self.use_label(loop)
            self.addop_j(loc, FOR_ITER, cleanup)
            if const_oparg == CONSTANT_BUILTIN_TUPLE:
                self.addop_i(loc, LIST_APPEND, 2)
                self.addop_j(loc, JUMP, loop)
            else:
                self.addop(loc, TO_BOOL)
                self.addop_j(loc, continue_jump_opcode, loop)
            self.addop(NO_LOCATION, POP_ITER)
            if const_oparg != CONSTANT_BUILTIN_TUPLE:
                self.addop_load_const(loc, False if initial_res is True else True)
            self.addop_j(loc, JUMP, end)
            self.use_label(cleanup)
            self.addop(NO_LOCATION, END_FOR)
            self.addop(NO_LOCATION, POP_ITER)
            if const_oparg == CONSTANT_BUILTIN_TUPLE:
                self.addop_i(loc, CALL_INTRINSIC_1, INTRINSIC_LIST_TO_TUPLE)
            else:
                self.addop_load_const(loc, initial_res)
            optimized = 1
            self.addop_j(loc, JUMP, end)
        self.use_label(skip_optimization)
        return optimized

    def maybe_optimize_method_call(self, e):
        meth = e.func
        args = e.args
        kwds = e.keywords
        if not isinstance(meth, ast.Attribute) or not isinstance(meth.ctx, ast.Load):
            return 0
        if self.is_import_originated(meth.value):
            return 0
        argsl = len(args)
        kwdsl = len(kwds)
        if argsl + kwdsl + (1 if kwdsl != 0 else 0) >= _PY_STACK_USE_GUIDELINE:
            return 0
        for elt in args:
            if isinstance(elt, ast.Starred):
                return 0
        for kw in kwds:
            if kw.arg is None:
                return 0
        loc = LOC(meth)
        if self.can_optimize_super_call(meth):
            self.load_args_for_super(meth.value)
            opcode = LOAD_SUPER_METHOD if len(meth.value.args) else LOAD_ZERO_SUPER_METHOD
            self.addop_name(loc, opcode, meth.attr, 'names')
            loc = self.update_start_location_to_match_attr(loc, meth)
            self.addop(loc, NOP)
        else:
            self.visit_expr(meth.value)
            loc = self.update_start_location_to_match_attr(loc, meth)
            self.addop_name(loc, LOAD_METHOD, meth.attr, 'names')
        for a in e.args:
            self.visit_expr(a)
        if kwdsl:
            for kw in kwds:
                self.visit_keyword(kw)
            self.call_simple_kw_helper(loc, kwds, kwdsl)
            loc = self.update_start_location_to_match_attr(LOC(e), meth)
            self.addop_i(loc, CALL_KW, argsl + kwdsl)
        else:
            loc = self.update_start_location_to_match_attr(LOC(e), meth)
            self.addop_i(loc, CALL, argsl)
        return 1

    def validate_keywords(self, keywords):
        nkeywords = len(keywords)
        for i in range(nkeywords):
            key = keywords[i]
            if key.arg is None:
                continue
            for j in range(i + 1, nkeywords):
                other = keywords[j]
                if other.arg is not None and key.arg == other.arg:
                    self.error(LOC(other), "keyword argument repeated: %s" % key.arg)

    def call(self, e):
        self.validate_keywords(e.keywords)
        if self.maybe_optimize_method_call(e) == 1:
            return
        skip_normal_call = self.new_label()
        self.check_caller(e.func)
        self.visit_expr(e.func)
        self.maybe_optimize_function_call(e, skip_normal_call)
        loc = LOC(e.func)
        self.addop(loc, PUSH_NULL)
        loc = LOC(e)
        self.call_helper(loc, 0, e.args, e.keywords)
        self.use_label(skip_normal_call)

    def template_str(self, e):
        loc = LOC(e)
        value_count = len(e.values)
        last_was_interpolation = 1
        stringslen = 0
        for value in e.values:
            if isinstance(value, ast.Interpolation):
                if last_was_interpolation:
                    self.addop_load_const(loc, "")
                    stringslen += 1
                last_was_interpolation = 1
            else:
                self.visit_expr(value)
                stringslen += 1
                last_was_interpolation = 0
        if last_was_interpolation:
            self.addop_load_const(loc, "")
            stringslen += 1
        self.addop_i(loc, BUILD_TUPLE, stringslen)
        interpolationslen = 0
        for value in e.values:
            if isinstance(value, ast.Interpolation):
                self.visit_expr(value)
                interpolationslen += 1
        self.addop_i(loc, BUILD_TUPLE, interpolationslen)
        self.addop(loc, BUILD_TEMPLATE)

    def joined_str(self, e):
        loc = LOC(e)
        value_count = len(e.values)
        if value_count > _PY_STACK_USE_GUIDELINE:
            self.addop_load_const(loc, "")
            self.addop_name(loc, LOAD_METHOD, "join", 'names')
            self.addop_i(loc, BUILD_LIST, 0)
            for v in e.values:
                self.visit_expr(v)
                self.addop_i(loc, LIST_APPEND, 1)
            self.addop_i(loc, CALL, 1)
        else:
            for v in e.values:
                self.visit_expr(v)
            if value_count > 1:
                self.addop_i(loc, BUILD_STRING, value_count)
            elif value_count == 0:
                self.addop_load_const(loc, "")

    def interpolation(self, e):
        loc = LOC(e)
        self.visit_expr(e.value)
        self.addop_load_const(loc, e.str)
        oparg = 2
        if e.format_spec is not None:
            oparg += 1
            self.visit_expr(e.format_spec)
        conversion = e.conversion
        if conversion != -1:
            if conversion == ord('s'):
                oparg |= FVC_STR << 2
            elif conversion == ord('r'):
                oparg |= FVC_REPR << 2
            elif conversion == ord('a'):
                oparg |= FVC_ASCII << 2
            else:
                raise SystemError("Unrecognized conversion character %d" % conversion)
        self.addop_i(loc, BUILD_INTERPOLATION, oparg)

    def formatted_value(self, e):
        conversion = e.conversion
        self.visit_expr(e.value)
        loc = LOC(e)
        if conversion != -1:
            if conversion == ord('s'):
                oparg = FVC_STR
            elif conversion == ord('r'):
                oparg = FVC_REPR
            elif conversion == ord('a'):
                oparg = FVC_ASCII
            else:
                raise SystemError("Unrecognized conversion character %d" % conversion)
            self.addop_i(loc, CONVERT_VALUE, oparg)
        if e.format_spec is not None:
            self.visit_expr(e.format_spec)
            self.addop(loc, FORMAT_WITH_SPEC)
        else:
            self.addop(loc, FORMAT_SIMPLE)

    def subkwargs(self, loc, keywords, begin, end):
        n = end - begin
        assert n > 0
        big = n * 2 > _PY_STACK_USE_GUIDELINE
        if big:
            self.addop_i(NO_LOCATION, BUILD_MAP, 0)
        for i in range(begin, end):
            kw = keywords[i]
            self.addop_load_const(loc, kw.arg)
            self.visit_expr(kw.value)
            if big:
                self.addop_i(NO_LOCATION, MAP_ADD, 1)
        if not big:
            self.addop_i(loc, BUILD_MAP, n)

    def call_simple_kw_helper(self, loc, keywords, nkwelts):
        names = tuple(kw.arg for kw in keywords[:nkwelts])
        self.addop_load_const(loc, names)

    def call_helper_impl(self, loc, n, args, injected_arg, keywords):
        self.validate_keywords(keywords)
        nelts = len(args)
        nkwelts = len(keywords)
        ex_call = False
        if nelts + nkwelts * 2 > _PY_STACK_USE_GUIDELINE:
            ex_call = True
        if not ex_call:
            for elt in args:
                if isinstance(elt, ast.Starred):
                    ex_call = True
                    break
        if not ex_call:
            for kw in keywords:
                if kw.arg is None:
                    ex_call = True
                    break
        if not ex_call:
            for elt in args:
                self.visit_expr(elt)
            if injected_arg is not None:
                self.nameop(loc, injected_arg, Load)
                nelts += 1
            if nkwelts:
                for kw in keywords:
                    self.visit_keyword(kw)
                self.call_simple_kw_helper(loc, keywords, nkwelts)
                self.addop_i(loc, CALL_KW, n + nelts + nkwelts)
            else:
                self.addop_i(loc, CALL, n + nelts)
            return
        # ex_call
        if n == 0 and nelts == 1 and isinstance(args[0], ast.Starred):
            self.visit_expr(args[0].value)
        else:
            self.starunpack_helper_impl(loc, args, injected_arg, n,
                                        BUILD_LIST, LIST_APPEND, LIST_EXTEND, 1)
        if nkwelts:
            have_dict = 0
            nseen = 0
            for i in range(nkwelts):
                kw = keywords[i]
                if kw.arg is None:
                    if nseen:
                        self.subkwargs(loc, keywords, i - nseen, i)
                        if have_dict:
                            self.addop_i(loc, DICT_MERGE, 1)
                        have_dict = 1
                        nseen = 0
                    if not have_dict:
                        self.addop_i(loc, BUILD_MAP, 0)
                        have_dict = 1
                    self.visit_expr(kw.value)
                    self.addop_i(loc, DICT_MERGE, 1)
                else:
                    nseen += 1
            if nseen:
                self.subkwargs(loc, keywords, nkwelts - nseen, nkwelts)
                if have_dict:
                    self.addop_i(loc, DICT_MERGE, 1)
                have_dict = 1
            assert have_dict
        if nkwelts == 0:
            self.addop(loc, PUSH_NULL)
        self.addop(loc, CALL_FUNCTION_EX)

    def call_helper(self, loc, n, args, keywords):
        self.call_helper_impl(loc, n, args, None, keywords)

    # -- comprehensions -----------------------------------------------------------

    def comprehension_generator(self, loc, generators, gen_index, depth, elt, val, type_,
                                iter_on_stack):
        gen = generators[gen_index]
        if gen.is_async:
            self.async_comprehension_generator(loc, generators, gen_index, depth, elt, val,
                                               type_, iter_on_stack)
        else:
            self.sync_comprehension_generator(loc, generators, gen_index, depth, elt, val,
                                              type_, iter_on_stack)

    def _comprehension_element(self, gen_index, generators, depth, elt, val, type_):
        elt_loc = LOC(elt)
        if gen_index >= len(generators):
            if type_ == COMP_GENEXP:
                self.visit_expr(elt)
                self.addop_yield(elt_loc)
                self.addop(elt_loc, POP_TOP)
            elif type_ == COMP_LISTCOMP:
                self.visit_expr(elt)
                self.addop_i(elt_loc, LIST_APPEND, depth + 1)
            elif type_ == COMP_SETCOMP:
                self.visit_expr(elt)
                self.addop_i(elt_loc, SET_ADD, depth + 1)
            elif type_ == COMP_DICTCOMP:
                self.visit_expr(elt)
                self.visit_expr(val)
                elt_loc = LOCATION(elt.lineno, val.end_lineno, elt.col_offset, val.end_col_offset)
                self.addop_i(elt_loc, MAP_ADD, depth + 1)
            else:
                raise SystemError("unknown comprehension type")
        return elt_loc

    def sync_comprehension_generator(self, loc, generators, gen_index, depth, elt, val, type_,
                                     iter_on_stack):
        start = self.new_label()
        if_cleanup = self.new_label()
        anchor = self.new_label()
        gen = generators[gen_index]
        if not iter_on_stack:
            if gen_index == 0:
                assert self.METADATA().u_argcount == 1
                self.addop_i(loc, LOAD_FAST, 0)
            else:
                elts = None
                if isinstance(gen.iter, ast.List):
                    elts = gen.iter.elts
                elif isinstance(gen.iter, ast.Tuple):
                    elts = gen.iter.elts
                if elts is not None and len(elts) == 1:
                    e0 = elts[0]
                    if not isinstance(e0, ast.Starred):
                        self.visit_expr(e0)
                        start = NO_LABEL
                if start != NO_LABEL:
                    self.visit_expr(gen.iter)
                    self.addop(LOC(gen.iter), GET_ITER)
        if start != NO_LABEL:
            depth += 1
            self.use_label(start)
            self.addop_j(LOC(gen.iter), FOR_ITER, anchor)
        self.visit_expr(gen.target)
        for e in gen.ifs:
            self.jump_if(loc, e, if_cleanup, 0)
        gen_index += 1
        if gen_index < len(generators):
            self.comprehension_generator(loc, generators, gen_index, depth, elt, val, type_, 0)
        elt_loc = self._comprehension_element(gen_index, generators, depth, elt, val, type_)
        self.use_label(if_cleanup)
        if start != NO_LABEL:
            self.addop_j(elt_loc, JUMP, start)
            self.use_label(anchor)
            self.addop(NO_LOCATION, END_FOR)
            self.addop(NO_LOCATION, POP_ITER)

    def async_comprehension_generator(self, loc, generators, gen_index, depth, elt, val, type_,
                                      iter_on_stack):
        start = self.new_label()
        send = self.new_label()
        except_ = self.new_label()
        if_cleanup = self.new_label()
        gen = generators[gen_index]
        if not iter_on_stack:
            if gen_index == 0:
                assert self.METADATA().u_argcount == 1
                self.addop_i(loc, LOAD_FAST, 0)
            else:
                self.visit_expr(gen.iter)
                self.addop(LOC(gen.iter), GET_AITER)
        self.use_label(start)
        self.push_fblock(loc, COMPILE_FBLOCK_ASYNC_COMPREHENSION_GENERATOR, start, NO_LABEL, None)
        self.addop_j(loc, SETUP_FINALLY, except_)
        self.addop(loc, GET_ANEXT)
        self.addop_load_const(loc, None)
        self.use_label(send)
        self.add_yield_from(loc, 1)
        self.addop(loc, POP_BLOCK)
        self.visit_expr(gen.target)
        for e in gen.ifs:
            self.jump_if(loc, e, if_cleanup, 0)
        depth += 1
        gen_index += 1
        if gen_index < len(generators):
            self.comprehension_generator(loc, generators, gen_index, depth, elt, val, type_, 0)
        elt_loc = self._comprehension_element(gen_index, generators, depth, elt, val, type_)
        self.use_label(if_cleanup)
        self.addop_j(elt_loc, JUMP, start)
        self.pop_fblock(COMPILE_FBLOCK_ASYNC_COMPREHENSION_GENERATOR, start)
        self.use_label(except_)
        self.addop_j(loc, END_ASYNC_FOR, send)

    def push_inlined_comprehension_locals(self, loc, comp, state):
        in_class_block = (self.SYMTABLE_ENTRY().type == ClassBlock) and not self.is_in_inlined_comp()
        outer = self.SYMTABLE_ENTRY()
        for k, v in list(comp.symbols.items()):
            symbol = v
            scope = symbol_to_scope(symbol)
            outsymbol = get_symbol(outer, k)
            outsc = symbol_to_scope(outsymbol)
            if (symbol & DEF_LOCAL and not (symbol & DEF_NONLOCAL)) or in_class_block:
                if state.pushed_locals is None:
                    state.pushed_locals = []
                self.addop_name(loc, LOAD_FAST_AND_CLEAR, k, 'varnames')
                if scope == CELL:
                    if outsc == FREE:
                        self.addop_name(loc, MAKE_CELL, k, 'freevars')
                    else:
                        self.addop_name(loc, MAKE_CELL, k, 'cellvars')
                state.pushed_locals.append(k)
        if state.pushed_locals:
            self.addop_i(loc, SWAP, len(state.pushed_locals) + 1)
            cleanup = self.new_label()
            state.cleanup = cleanup
            self.addop_j(loc, SETUP_FINALLY, cleanup)

    def push_inlined_comprehension_state(self, loc, comp, state):
        self.tweak_inlined_comprehension_scopes(loc, comp, state)
        self.push_inlined_comprehension_locals(loc, comp, state)

    def restore_inlined_comprehension_locals(self, loc, state):
        npops = len(state.pushed_locals)
        self.addop_i(loc, SWAP, npops + 1)
        for i in range(npops - 1, -1, -1):
            k = state.pushed_locals[i]
            self.addop_name(loc, STORE_FAST_MAYBE_NULL, k, 'varnames')

    def pop_inlined_comprehension_locals(self, loc, state):
        if state.pushed_locals:
            self.addop(NO_LOCATION, POP_BLOCK)
            end = self.new_label()
            self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, end)
            self.use_label(state.cleanup)
            self.addop_i(NO_LOCATION, SWAP, 2)
            self.addop(NO_LOCATION, POP_TOP)
            self.restore_inlined_comprehension_locals(loc, state)
            self.addop_i(NO_LOCATION, RERAISE, 0)
            self.use_label(end)
            self.restore_inlined_comprehension_locals(loc, state)
            state.pushed_locals = None

    def pop_inlined_comprehension_state(self, loc, state):
        self.pop_inlined_comprehension_locals(loc, state)
        self.revert_inlined_comprehension_scopes(loc, state)

    def comprehension_iter(self, comp):
        self.visit_expr(comp.iter)
        if comp.is_async:
            self.addop(LOC(comp.iter), GET_AITER)
        else:
            self.addop(LOC(comp.iter), GET_ITER)

    def comprehension(self, e, type_, name, generators, elt, val):
        inline_state = InlinedComprehensionState()
        entry = self.c_st.blocks[key_of(e)]
        is_inlined = entry.comp_inlined
        is_async_comprehension = entry.coroutine
        loc = LOC(e)
        outermost = generators[0]
        if is_inlined:
            self.comprehension_iter(outermost)
            self.push_inlined_comprehension_state(loc, entry, inline_state)
        else:
            umd = CodeUnitMetadata()
            umd.u_argcount = 1
            self.codegen_enter_scope(name, COMPILE_SCOPE_COMPREHENSION, key_of(e), e.lineno,
                                     None, umd)
        try:
            if type_ != COMP_GENEXP:
                if type_ == COMP_LISTCOMP:
                    op_ = BUILD_LIST
                elif type_ == COMP_SETCOMP:
                    op_ = BUILD_SET
                elif type_ == COMP_DICTCOMP:
                    op_ = BUILD_MAP
                else:
                    raise SystemError("unknown comprehension type %d" % type_)
                self.addop_i(loc, op_, 0)
                if is_inlined:
                    self.addop_i(loc, SWAP, 2)
            self.comprehension_generator(loc, generators, 0, 0, elt, val, type_, is_inlined)
            if is_inlined:
                self.pop_inlined_comprehension_state(loc, inline_state)
                return
            if type_ != COMP_GENEXP:
                self.addop(LOC(e), RETURN_VALUE)
            if type_ == COMP_GENEXP:
                self.wrap_in_stopiteration_handler()
            co = self.optimize_and_assemble(1)
        except BaseException:
            if not is_inlined:
                self.exit_scope()
            raise
        self.exit_scope()
        loc = LOC(e)
        self.make_closure(loc, co, 0)
        self.comprehension_iter(outermost)
        self.addop_i(loc, CALL, 0)
        if is_async_comprehension and type_ != COMP_GENEXP:
            self.addop_i(loc, GET_AWAITABLE, 0)
            self.addop_load_const(loc, None)
            self.add_yield_from(loc, 1)

    def genexp(self, e):
        self.comprehension(e, COMP_GENEXP, "<genexpr>", e.generators, e.elt, None)

    def listcomp(self, e):
        self.comprehension(e, COMP_LISTCOMP, "<listcomp>", e.generators, e.elt, None)

    def setcomp(self, e):
        self.comprehension(e, COMP_SETCOMP, "<setcomp>", e.generators, e.elt, None)

    def dictcomp(self, e):
        self.comprehension(e, COMP_DICTCOMP, "<dictcomp>", e.generators, e.key, e.value)

    def visit_keyword(self, k):
        self.visit_expr(k.value)

    # -- with ---------------------------------------------------------------------

    def with_except_finish(self, cleanup):
        suppress = self.new_label()
        self.addop(NO_LOCATION, TO_BOOL)
        self.addop_j(NO_LOCATION, POP_JUMP_IF_TRUE, suppress)
        self.addop_i(NO_LOCATION, RERAISE, 2)
        self.use_label(suppress)
        self.addop(NO_LOCATION, POP_TOP)
        self.addop(NO_LOCATION, POP_BLOCK)
        self.addop(NO_LOCATION, POP_EXCEPT)
        self.addop(NO_LOCATION, POP_TOP)
        self.addop(NO_LOCATION, POP_TOP)
        self.addop(NO_LOCATION, POP_TOP)
        exit_ = self.new_label()
        self.addop_j(NO_LOCATION, JUMP_NO_INTERRUPT, exit_)
        self.use_label(cleanup)
        self.pop_except_and_reraise(NO_LOCATION)
        self.use_label(exit_)

    def async_with_inner(self, s, pos):
        loc = LOC(s)
        item = s.items[pos]
        block = self.new_label()
        final = self.new_label()
        exit_ = self.new_label()
        cleanup = self.new_label()
        self.visit_expr(item.context_expr)
        loc = LOC(item.context_expr)
        self.addop_i(loc, COPY, 1)
        self.addop_i(loc, LOAD_SPECIAL, SPECIAL___AEXIT__)
        self.addop_i(loc, SWAP, 2)
        self.addop_i(loc, SWAP, 3)
        self.addop_i(loc, LOAD_SPECIAL, SPECIAL___AENTER__)
        self.addop_i(loc, CALL, 0)
        self.addop_i(loc, GET_AWAITABLE, 1)
        self.addop_load_const(loc, None)
        self.add_yield_from(loc, 1)
        self.addop_j(loc, SETUP_WITH, final)
        self.use_label(block)
        self.push_fblock(loc, COMPILE_FBLOCK_ASYNC_WITH, block, final, s)
        if item.optional_vars is not None:
            self.visit_expr(item.optional_vars)
        else:
            self.addop(loc, POP_TOP)
        pos += 1
        if pos == len(s.items):
            self.visit_stmts(s.body)
        else:
            self.async_with_inner(s, pos)
        self.pop_fblock(COMPILE_FBLOCK_ASYNC_WITH, block)
        self.addop(loc, POP_BLOCK)
        self.call_exit_with_nones(loc)
        self.addop_i(loc, GET_AWAITABLE, 2)
        self.addop_load_const(loc, None)
        self.add_yield_from(loc, 1)
        self.addop(loc, POP_TOP)
        self.addop_j(loc, JUMP, exit_)
        self.use_label(final)
        self.addop_j(loc, SETUP_CLEANUP, cleanup)
        self.addop(loc, PUSH_EXC_INFO)
        self.addop(loc, WITH_EXCEPT_START)
        self.addop_i(loc, GET_AWAITABLE, 2)
        self.addop_load_const(loc, None)
        self.add_yield_from(loc, 1)
        self.with_except_finish(cleanup)
        self.use_label(exit_)

    def async_with(self, s):
        self.async_with_inner(s, 0)

    def with_inner(self, s, pos):
        item = s.items[pos]
        block = self.new_label()
        final = self.new_label()
        exit_ = self.new_label()
        cleanup = self.new_label()
        self.visit_expr(item.context_expr)
        loc = LOC(item.context_expr)
        self.addop_i(loc, COPY, 1)
        self.addop_i(loc, LOAD_SPECIAL, SPECIAL___EXIT__)
        self.addop_i(loc, SWAP, 2)
        self.addop_i(loc, SWAP, 3)
        self.addop_i(loc, LOAD_SPECIAL, SPECIAL___ENTER__)
        self.addop_i(loc, CALL, 0)
        self.addop_j(loc, SETUP_WITH, final)
        self.use_label(block)
        self.push_fblock(loc, COMPILE_FBLOCK_WITH, block, final, s)
        if item.optional_vars is not None:
            self.visit_expr(item.optional_vars)
        else:
            self.addop(loc, POP_TOP)
        pos += 1
        if pos == len(s.items):
            self.visit_stmts(s.body)
        else:
            self.with_inner(s, pos)
        self.addop(NO_LOCATION, POP_BLOCK)
        self.pop_fblock(COMPILE_FBLOCK_WITH, block)
        self.call_exit_with_nones(loc)
        self.addop(loc, POP_TOP)
        self.addop_j(loc, JUMP, exit_)
        self.use_label(final)
        self.addop_j(loc, SETUP_CLEANUP, cleanup)
        self.addop(loc, PUSH_EXC_INFO)
        self.addop(loc, WITH_EXCEPT_START)
        self.with_except_finish(cleanup)
        self.use_label(exit_)

    def with_(self, s):
        self.with_inner(s, 0)

    # -- expressions ----------------------------------------------------------------

    def visit_expr(self, e):
        loc = LOC(e)
        t = type(e)
        if t is ast.NamedExpr:
            self.visit_expr(e.value)
            self.addop_i(loc, COPY, 1)
            self.visit_expr(e.target)
        elif t is ast.BoolOp:
            self.boolop(e)
        elif t is ast.BinOp:
            self.visit_expr(e.left)
            self.visit_expr(e.right)
            self.addop_binary(loc, e.op, False)
        elif t is ast.UnaryOp:
            self.visit_expr(e.operand)
            if isinstance(e.op, ast.UAdd):
                self.addop_i(loc, CALL_INTRINSIC_1, INTRINSIC_UNARY_POSITIVE)
            elif isinstance(e.op, ast.Not):
                self.addop(loc, TO_BOOL)
                self.addop(loc, UNARY_NOT)
            elif isinstance(e.op, ast.Invert):
                self.addop(loc, UNARY_INVERT)
            elif isinstance(e.op, ast.USub):
                self.addop(loc, UNARY_NEGATIVE)
            else:
                raise SystemError("unary op should not be possible")
        elif t is ast.Lambda:
            self.lambda_(e)
        elif t is ast.IfExp:
            self.ifexp(e)
        elif t is ast.Dict:
            self.dict_(e)
        elif t is ast.Set:
            self.set_(e)
        elif t is ast.GeneratorExp:
            self.genexp(e)
        elif t is ast.ListComp:
            self.listcomp(e)
        elif t is ast.SetComp:
            self.setcomp(e)
        elif t is ast.DictComp:
            self.dictcomp(e)
        elif t is ast.Yield:
            if not is_function_like(self.SYMTABLE_ENTRY()):
                self.error(loc, "'yield' outside function")
            if e.value is not None:
                self.visit_expr(e.value)
            else:
                self.addop_load_const(loc, None)
            self.addop_yield(loc)
        elif t is ast.YieldFrom:
            if not is_function_like(self.SYMTABLE_ENTRY()):
                self.error(loc, "'yield from' outside function")
            if self.SCOPE_TYPE() == COMPILE_SCOPE_ASYNC_FUNCTION:
                self.error(loc, "'yield from' inside async function")
            self.visit_expr(e.value)
            self.addop(loc, GET_YIELD_FROM_ITER)
            self.addop_load_const(loc, None)
            self.add_yield_from(loc, 0)
        elif t is ast.Await:
            self.visit_expr(e.value)
            self.addop_i(loc, GET_AWAITABLE, 0)
            self.addop_load_const(loc, None)
            self.add_yield_from(loc, 1)
        elif t is ast.Compare:
            self.compare(e)
        elif t is ast.Call:
            self.call(e)
        elif t is ast.Constant:
            self.addop_load_const(loc, e.value)
        elif t is ast.JoinedStr:
            self.joined_str(e)
        elif t is ast.TemplateStr:
            self.template_str(e)
        elif t is ast.FormattedValue:
            self.formatted_value(e)
        elif t is ast.Interpolation:
            self.interpolation(e)
        elif t is ast.Attribute:
            if isinstance(e.ctx, ast.Load):
                if self.can_optimize_super_call(e):
                    self.load_args_for_super(e.value)
                    opcode = LOAD_SUPER_ATTR if len(e.value.args) else LOAD_ZERO_SUPER_ATTR
                    self.addop_name(loc, opcode, e.attr, 'names')
                    loc = self.update_start_location_to_match_attr(loc, e)
                    self.addop(loc, NOP)
                    return
            self.maybe_add_static_attribute_to_class(e)
            self.visit_expr(e.value)
            loc = LOC(e)
            loc = self.update_start_location_to_match_attr(loc, e)
            if isinstance(e.ctx, ast.Load):
                self.addop_name(loc, LOAD_ATTR, e.attr, 'names')
            elif isinstance(e.ctx, ast.Store):
                self.addop_name(loc, STORE_ATTR, e.attr, 'names')
            else:
                self.addop_name(loc, DELETE_ATTR, e.attr, 'names')
        elif t is ast.Subscript:
            self.subscript(e)
        elif t is ast.Starred:
            if isinstance(e.ctx, ast.Store):
                self.error(loc, "starred assignment target must be in a list or tuple")
            else:
                self.error(loc, "can't use starred expression here")
        elif t is ast.Slice:
            self.slice_(e)
        elif t is ast.Name:
            self.nameop(loc, e.id, e.ctx)
        elif t is ast.List:
            self.list_(e)
        elif t is ast.Tuple:
            self.tuple_(e)
        else:
            raise SystemError("unknown expression kind %s" % t.__name__)

    def augassign(self, s):
        e = s.target
        loc = LOC(e)
        if isinstance(e, ast.Attribute):
            self.visit_expr(e.value)
            self.addop_i(loc, COPY, 1)
            loc = self.update_start_location_to_match_attr(loc, e)
            self.addop_name(loc, LOAD_ATTR, e.attr, 'names')
        elif isinstance(e, ast.Subscript):
            self.visit_expr(e.value)
            if should_apply_two_element_slice_optimization(e.slice):
                self.slice_two_parts(e.slice)
                self.addop_i(loc, COPY, 3)
                self.addop_i(loc, COPY, 3)
                self.addop_i(loc, COPY, 3)
                self.addop(loc, BINARY_SLICE)
            else:
                self.visit_expr(e.slice)
                self.addop_i(loc, COPY, 2)
                self.addop_i(loc, COPY, 2)
                self.addop_i(loc, BINARY_OP, NB_SUBSCR)
        elif isinstance(e, ast.Name):
            self.nameop(loc, e.id, Load)
        else:
            raise SystemError("invalid node type (%s) for augmented assignment" % type(e).__name__)
        loc = LOC(s)
        self.visit_expr(s.value)
        self.addop_binary(loc, s.op, True)
        loc = LOC(e)
        if isinstance(e, ast.Attribute):
            loc = self.update_start_location_to_match_attr(loc, e)
            self.addop_i(loc, SWAP, 2)
            self.addop_name(loc, STORE_ATTR, e.attr, 'names')
        elif isinstance(e, ast.Subscript):
            if should_apply_two_element_slice_optimization(e.slice):
                self.addop_i(loc, SWAP, 4)
                self.addop_i(loc, SWAP, 3)
                self.addop_i(loc, SWAP, 2)
                self.addop(loc, STORE_SLICE)
            else:
                self.addop_i(loc, SWAP, 3)
                self.addop_i(loc, SWAP, 2)
                self.addop(loc, STORE_SUBSCR)
        else:
            self.nameop(loc, e.id, Store)

    def check_ann_expr(self, e):
        self.visit_expr(e)
        self.addop(LOC(e), POP_TOP)

    def check_annotation(self, s):
        if self.FUTURE_FEATURES() & CO_FUTURE_ANNOTATIONS:
            return
        if self.SCOPE_TYPE() in (COMPILE_SCOPE_MODULE, COMPILE_SCOPE_CLASS):
            self.check_ann_expr(s.annotation)

    def check_ann_subscr(self, e):
        if isinstance(e, ast.Slice):
            if e.lower is not None:
                self.check_ann_expr(e.lower)
            if e.upper is not None:
                self.check_ann_expr(e.upper)
            if e.step is not None:
                self.check_ann_expr(e.step)
        elif isinstance(e, ast.Tuple):
            for elt in e.elts:
                self.check_ann_subscr(elt)
        else:
            self.check_ann_expr(e)

    def annassign(self, s):
        loc = LOC(s)
        targ = s.target
        future_annotations = self.FUTURE_FEATURES() & CO_FUTURE_ANNOTATIONS
        if s.value is not None:
            self.visit_expr(s.value)
            self.visit_expr(targ)
        if isinstance(targ, ast.Name):
            if s.simple and self.SCOPE_TYPE() in (COMPILE_SCOPE_MODULE, COMPILE_SCOPE_CLASS):
                if future_annotations:
                    self.visit_annexpr(s.annotation)
                    self.addop_name(loc, LOAD_NAME, "__annotations__", 'names')
                    mangled = self.maybe_mangle_name(targ.id)
                    self.addop_load_const(loc, mangled)
                    self.addop(loc, STORE_SUBSCR)
                else:
                    conditional_annotation_index = self.add_deferred_annotation(s)
                    if conditional_annotation_index is not None:
                        if self.SCOPE_TYPE() == COMPILE_SCOPE_CLASS:
                            self.addop_name(loc, LOAD_DEREF, "__conditional_annotations__",
                                            'cellvars')
                        else:
                            self.addop_name(loc, LOAD_NAME, "__conditional_annotations__",
                                            'names')
                        self.addop_load_const(loc, conditional_annotation_index)
                        self.addop_i(loc, SET_ADD, 1)
                        self.addop(loc, POP_TOP)
        elif isinstance(targ, ast.Attribute):
            if s.value is None:
                self.check_ann_expr(targ.value)
        elif isinstance(targ, ast.Subscript):
            if s.value is None:
                self.check_ann_expr(targ.value)
                self.check_ann_subscr(targ.slice)
        else:
            raise SystemError("invalid node type (%s) for annotated assignment"
                              % type(targ).__name__)
        if future_annotations and not s.simple:
            self.check_annotation(s)

    def subscript(self, e):
        loc = LOC(e)
        ctx = e.ctx
        if isinstance(ctx, ast.Load):
            self.check_subscripter(e.value)
            self.check_index(e.value, e.slice)
        self.visit_expr(e.value)
        if (should_apply_two_element_slice_optimization(e.slice)
                and not isinstance(ctx, ast.Del)):
            self.slice_two_parts(e.slice)
            if isinstance(ctx, ast.Load):
                self.addop(loc, BINARY_SLICE)
            else:
                assert isinstance(ctx, ast.Store)
                self.addop(loc, STORE_SLICE)
        else:
            self.visit_expr(e.slice)
            if isinstance(ctx, ast.Load):
                self.addop_i(loc, BINARY_OP, NB_SUBSCR)
            elif isinstance(ctx, ast.Store):
                self.addop(loc, STORE_SUBSCR)
            else:
                self.addop(loc, DELETE_SUBSCR)

    def slice_two_parts(self, s):
        if s.lower is not None:
            self.visit_expr(s.lower)
        else:
            self.addop_load_const(LOC(s), None)
        if s.upper is not None:
            self.visit_expr(s.upper)
        else:
            self.addop_load_const(LOC(s), None)

    def slice_(self, s):
        n = 2
        if is_constant_slice(s):
            start = s.lower.value if s.lower is not None else None
            stop = s.upper.value if s.upper is not None else None
            step = s.step.value if s.step is not None else None
            self.addop_load_const(LOC(s), slice(start, stop, step))
            return
        self.slice_two_parts(s)
        if s.step is not None:
            n += 1
            self.visit_expr(s.step)
        self.addop_i(LOC(s), BUILD_SLICE, n)

    # -- pattern matching ------------------------------------------------------------

    def ensure_fail_pop(self, pc, n):
        size = n + 1
        if size <= pc.fail_pop_size:
            return
        if pc.fail_pop is None:
            pc.fail_pop = []
        while pc.fail_pop_size < size:
            pc.fail_pop.append(self.new_label())
            pc.fail_pop_size += 1

    def jump_to_fail_pop(self, loc, pc, op_):
        pops = pc.on_top + len(pc.stores)
        self.ensure_fail_pop(pc, pops)
        self.addop_j(loc, op_, pc.fail_pop[pops])

    def emit_and_reset_fail_pop(self, loc, pc):
        if not pc.fail_pop_size:
            assert pc.fail_pop is None
            return
        pc.fail_pop_size -= 1
        while pc.fail_pop_size:
            self.use_label(pc.fail_pop[pc.fail_pop_size])
            try:
                self.INSTR_SEQUENCE().addop(POP_TOP, 0, loc)
            except BaseException:
                pc.fail_pop_size = 0
                pc.fail_pop = None
                raise
            pc.fail_pop_size -= 1
        self.use_label(pc.fail_pop[0])
        pc.fail_pop = None

    def error_duplicate_store(self, loc, n):
        self.error(loc, "multiple assignments to name %r in pattern" % n)

    def pattern_helper_rotate(self, loc, count):
        while 1 < count:
            self.addop_i(loc, SWAP, count)
            count -= 1

    def pattern_helper_store_name(self, loc, n, pc):
        if n is None:
            self.addop(loc, POP_TOP)
            return
        if n in pc.stores:
            self.error_duplicate_store(loc, n)
        rotations = pc.on_top + len(pc.stores) + 1
        self.pattern_helper_rotate(loc, rotations)
        pc.stores.append(n)

    def pattern_unpack_helper(self, loc, elts):
        n = len(elts)
        seen_star = 0
        for i, elt in enumerate(elts):
            if isinstance(elt, ast.MatchStar) and not seen_star:
                if i >= (1 << 8) or (n - i - 1) >= ((1 << 31) - 1) >> 8:
                    self.error(loc, "too many expressions in star-unpacking sequence pattern")
                self.addop_i(loc, UNPACK_EX, (i + ((n - i - 1) << 8)))
                seen_star = 1
            elif isinstance(elt, ast.MatchStar):
                self.error(loc, "multiple starred expressions in sequence pattern")
        if not seen_star:
            self.addop_i(loc, UNPACK_SEQUENCE, n)

    def pattern_helper_sequence_unpack(self, loc, patterns, star, pc):
        self.pattern_unpack_helper(loc, patterns)
        size = len(patterns)
        pc.on_top += size
        for pattern in patterns:
            pc.on_top -= 1
            self.pattern_subpattern(pattern, pc)

    def pattern_helper_sequence_subscr(self, loc, patterns, star, pc):
        pc.on_top += 1
        size = len(patterns)
        for i, pattern in enumerate(patterns):
            if WILDCARD_CHECK(pattern):
                continue
            if i == star:
                assert WILDCARD_STAR_CHECK(pattern)
                continue
            self.addop_i(loc, COPY, 1)
            if i < star:
                self.addop_load_const(loc, i)
            else:
                self.addop(loc, GET_LEN)
                self.addop_load_const(loc, size - i)
                self.addop_binary(loc, ast.Sub_singleton, False)
            self.addop_i(loc, BINARY_OP, NB_SUBSCR)
            self.pattern_subpattern(pattern, pc)
        pc.on_top -= 1
        self.addop(loc, POP_TOP)

    def pattern_subpattern(self, p, pc):
        allow_irrefutable = pc.allow_irrefutable
        pc.allow_irrefutable = 1
        self.pattern(p, pc)
        pc.allow_irrefutable = allow_irrefutable

    def pattern_as(self, p, pc):
        if p.pattern is None:
            if not pc.allow_irrefutable:
                if p.name is not None:
                    self.error(LOC(p), "name capture %r makes remaining patterns unreachable"
                               % p.name)
                self.error(LOC(p), "wildcard makes remaining patterns unreachable")
            self.pattern_helper_store_name(LOC(p), p.name, pc)
            return
        pc.on_top += 1
        self.addop_i(LOC(p), COPY, 1)
        self.pattern(p.pattern, pc)
        pc.on_top -= 1
        self.pattern_helper_store_name(LOC(p), p.name, pc)

    def pattern_star(self, p, pc):
        self.pattern_helper_store_name(LOC(p), p.name, pc)

    def validate_kwd_attrs(self, attrs, patterns):
        nattrs = len(attrs)
        for i in range(nattrs):
            attr = attrs[i]
            for j in range(i + 1, nattrs):
                other = attrs[j]
                if attr == other:
                    loc = LOC(patterns[j])
                    self.error(loc, "attribute name repeated in class pattern: %s" % attr)

    def pattern_class(self, p, pc):
        patterns = p.patterns
        kwd_attrs = p.kwd_attrs
        kwd_patterns = p.kwd_patterns
        nargs = len(patterns)
        nattrs = len(kwd_attrs)
        nkwd_patterns = len(kwd_patterns)
        if nattrs != nkwd_patterns:
            self.error(LOC(p), "kwd_attrs (%d) / kwd_patterns (%d) length mismatch in class pattern"
                       % (nattrs, nkwd_patterns))
        INT_MAX = (1 << 31) - 1
        if INT_MAX < nargs or INT_MAX < nargs + nattrs - 1:
            self.error(LOC(p), "too many sub-patterns in class pattern %r" % p.cls)
        if nattrs:
            self.validate_kwd_attrs(kwd_attrs, kwd_patterns)
        self.visit_expr(p.cls)
        attr_names = tuple(kwd_attrs)
        self.addop_load_const(LOC(p), attr_names)
        self.addop_i(LOC(p), MATCH_CLASS, nargs)
        self.addop_i(LOC(p), COPY, 1)
        self.addop_load_const(LOC(p), None)
        self.addop_i(LOC(p), IS_OP, 1)
        pc.on_top += 1
        self.jump_to_fail_pop(LOC(p), pc, POP_JUMP_IF_FALSE)
        self.addop_i(LOC(p), UNPACK_SEQUENCE, nargs + nattrs)
        pc.on_top += nargs + nattrs - 1
        for i in range(nargs + nattrs):
            pc.on_top -= 1
            if i < nargs:
                pattern = patterns[i]
            else:
                pattern = kwd_patterns[i - nargs]
            if WILDCARD_CHECK(pattern):
                self.addop(LOC(p), POP_TOP)
                continue
            self.pattern_subpattern(pattern, pc)

    def pattern_mapping_key(self, seen, p, i):
        keys = p.keys
        patterns = p.patterns
        key = keys[i]
        if key is None:
            loc = LOC(patterns[i])
            self.error(loc, "can't use NULL keys in MatchMapping (set 'rest' parameter instead)")
        if isinstance(key, ast.Constant):
            if key.value in seen:
                self.error(LOC(p), "mapping pattern checks duplicate key (%r)" % (key.value,))
            seen.add(key.value)
        elif not isinstance(key, ast.Attribute):
            self.error(LOC(p), "mapping pattern keys may only match literals and attribute lookups")
        self.visit_expr(key)

    def pattern_mapping(self, p, pc):
        keys = p.keys
        patterns = p.patterns
        size = len(keys)
        npatterns = len(patterns)
        if size != npatterns:
            self.error(LOC(p), "keys (%d) / patterns (%d) length mismatch in mapping pattern"
                       % (size, npatterns))
        star_target = p.rest
        pc.on_top += 1
        self.addop(LOC(p), MATCH_MAPPING)
        self.jump_to_fail_pop(LOC(p), pc, POP_JUMP_IF_FALSE)
        if not size and star_target is None:
            pc.on_top -= 1
            self.addop(LOC(p), POP_TOP)
            return
        if size:
            self.addop(LOC(p), GET_LEN)
            self.addop_load_const(LOC(p), size)
            self.addop_compare(LOC(p), ast.GtE_singleton)
            self.jump_to_fail_pop(LOC(p), pc, POP_JUMP_IF_FALSE)
        if ((1 << 31) - 1) < size - 1:
            self.error(LOC(p), "too many sub-patterns in mapping pattern")
        seen = _ConstSet()
        for i in range(size):
            self.pattern_mapping_key(seen, p, i)
        self.addop_i(LOC(p), BUILD_TUPLE, size)
        self.addop(LOC(p), MATCH_KEYS)
        pc.on_top += 2
        self.addop_i(LOC(p), COPY, 1)
        self.addop_load_const(LOC(p), None)
        self.addop_i(LOC(p), IS_OP, 1)
        self.jump_to_fail_pop(LOC(p), pc, POP_JUMP_IF_FALSE)
        self.addop_i(LOC(p), UNPACK_SEQUENCE, size)
        pc.on_top += size - 1
        for i in range(size):
            pc.on_top -= 1
            self.pattern_subpattern(patterns[i], pc)
        pc.on_top -= 2
        if star_target is not None:
            self.addop_i(LOC(p), BUILD_MAP, 0)
            self.addop_i(LOC(p), SWAP, 3)
            self.addop_i(LOC(p), DICT_UPDATE, 2)
            self.addop_i(LOC(p), UNPACK_SEQUENCE, size)
            while size:
                self.addop_i(LOC(p), COPY, 1 + size)
                size -= 1
                self.addop_i(LOC(p), SWAP, 2)
                self.addop(LOC(p), DELETE_SUBSCR)
            self.pattern_helper_store_name(LOC(p), star_target, pc)
        else:
            self.addop(LOC(p), POP_TOP)
            self.addop(LOC(p), POP_TOP)

    def pattern_or(self, p, pc):
        end = self.new_label()
        size = len(p.patterns)
        assert size > 1
        old_pc = pc.copy()
        control = None
        for i, alt in enumerate(p.patterns):
            pc.stores = []
            pc.allow_irrefutable = (i == size - 1) and old_pc.allow_irrefutable
            pc.fail_pop = None
            pc.fail_pop_size = 0
            pc.on_top = 0
            self.addop_i(LOC(alt), COPY, 1)
            self.pattern(alt, pc)
            nstores = len(pc.stores)
            if not i:
                assert control is None
                control = pc.stores
            elif nstores != len(control):
                self.error(LOC(p), "alternative patterns bind different names")
            elif nstores:
                icontrol = nstores
                while icontrol:
                    icontrol -= 1
                    name = control[icontrol]
                    try:
                        istores = pc.stores.index(name)
                    except ValueError:
                        self.error(LOC(p), "alternative patterns bind different names")
                    if icontrol != istores:
                        assert istores < icontrol
                        rotations = istores + 1
                        rotated = pc.stores[:rotations]
                        del pc.stores[:rotations]
                        pc.stores[icontrol - istores:icontrol - istores] = rotated
                        while rotations:
                            rotations -= 1
                            self.pattern_helper_rotate(LOC(alt), icontrol + 1)
            assert control is not None
            self.addop_j(LOC(alt), JUMP, end)
            self.emit_and_reset_fail_pop(LOC(alt), pc)
        pc.stores = old_pc.stores
        pc.allow_irrefutable = old_pc.allow_irrefutable
        pc.fail_pop = old_pc.fail_pop
        pc.fail_pop_size = old_pc.fail_pop_size
        pc.on_top = old_pc.on_top
        self.addop(LOC(p), POP_TOP)
        self.jump_to_fail_pop(LOC(p), pc, JUMP)
        self.use_label(end)
        nstores = len(control)
        nrots = nstores + 1 + pc.on_top + len(pc.stores)
        for i in range(nstores):
            self.pattern_helper_rotate(LOC(p), nrots)
            name = control[i]
            if name in pc.stores:
                self.error_duplicate_store(LOC(p), name)
            pc.stores.append(name)
        self.addop(LOC(p), POP_TOP)

    def pattern_sequence(self, p, pc):
        patterns = p.patterns
        size = len(patterns)
        star = -1
        only_wildcard = 1
        star_wildcard = 0
        for i, pattern in enumerate(patterns):
            if isinstance(pattern, ast.MatchStar):
                if star >= 0:
                    self.error(LOC(p), "multiple starred names in sequence pattern")
                star_wildcard = 1 if WILDCARD_STAR_CHECK(pattern) else 0
                only_wildcard &= star_wildcard
                star = i
                continue
            only_wildcard &= 1 if WILDCARD_CHECK(pattern) else 0
        pc.on_top += 1
        self.addop(LOC(p), MATCH_SEQUENCE)
        self.jump_to_fail_pop(LOC(p), pc, POP_JUMP_IF_FALSE)
        if star < 0:
            self.addop(LOC(p), GET_LEN)
            self.addop_load_const(LOC(p), size)
            self.addop_compare(LOC(p), ast.Eq_singleton)
            self.jump_to_fail_pop(LOC(p), pc, POP_JUMP_IF_FALSE)
        elif size > 1:
            self.addop(LOC(p), GET_LEN)
            self.addop_load_const(LOC(p), size - 1)
            self.addop_compare(LOC(p), ast.GtE_singleton)
            self.jump_to_fail_pop(LOC(p), pc, POP_JUMP_IF_FALSE)
        pc.on_top -= 1
        if only_wildcard:
            self.addop(LOC(p), POP_TOP)
        elif star_wildcard:
            self.pattern_helper_sequence_subscr(LOC(p), patterns, star, pc)
        else:
            self.pattern_helper_sequence_unpack(LOC(p), patterns, star, pc)

    def pattern_value(self, p, pc):
        value = p.value
        if not MATCH_VALUE_EXPR(value):
            self.error(LOC(p), "patterns may only match literals and attribute lookups")
        self.visit_expr(value)
        self.addop_compare(LOC(p), ast.Eq_singleton)
        self.addop(LOC(p), TO_BOOL)
        self.jump_to_fail_pop(LOC(p), pc, POP_JUMP_IF_FALSE)

    def pattern_singleton(self, p, pc):
        self.addop_load_const(LOC(p), p.value)
        self.addop_compare(LOC(p), ast.Is_singleton)
        self.jump_to_fail_pop(LOC(p), pc, POP_JUMP_IF_FALSE)

    def pattern(self, p, pc):
        t = type(p)
        if t is ast.MatchValue:
            self.pattern_value(p, pc)
        elif t is ast.MatchSingleton:
            self.pattern_singleton(p, pc)
        elif t is ast.MatchSequence:
            self.pattern_sequence(p, pc)
        elif t is ast.MatchMapping:
            self.pattern_mapping(p, pc)
        elif t is ast.MatchClass:
            self.pattern_class(p, pc)
        elif t is ast.MatchStar:
            self.pattern_star(p, pc)
        elif t is ast.MatchAs:
            self.pattern_as(p, pc)
        elif t is ast.MatchOr:
            self.pattern_or(p, pc)
        else:
            self.error(LOC(p), "invalid match pattern node in AST (kind=%s)" % t.__name__)

    def match_inner(self, s, pc):
        self.visit_expr(s.subject)
        end = self.new_label()
        cases = len(s.cases)
        assert cases > 0
        m = s.cases[cases - 1]
        has_default = 1 if (WILDCARD_CHECK(m.pattern) and 1 < cases) else 0
        for i in range(cases - has_default):
            m = s.cases[i]
            if i != cases - has_default - 1:
                self.addop_i(LOC(m.pattern), COPY, 1)
            pc.stores = []
            pc.allow_irrefutable = m.guard is not None or i == cases - 1
            pc.fail_pop = None
            pc.fail_pop_size = 0
            pc.on_top = 0
            self.pattern(m.pattern, pc)
            assert not pc.on_top
            for name in pc.stores:
                self.nameop(LOC(m.pattern), name, Store)
            if m.guard is not None:
                self.ensure_fail_pop(pc, 0)
                self.jump_if(LOC(m.pattern), m.guard, pc.fail_pop[0], 0)
            if i != cases - has_default - 1:
                self.addop(NEXT_LOCATION, POP_TOP)
            self.visit_stmts(m.body)
            self.addop_j(NO_LOCATION, JUMP, end)
            self.emit_and_reset_fail_pop(LOC(m.pattern), pc)
        if has_default:
            m = s.cases[cases - 1]
            if cases == 1:
                self.addop(LOC(m.pattern), POP_TOP)
            else:
                self.addop(LOC(m.pattern), NOP)
            if m.guard is not None:
                self.jump_if(LOC(m.pattern), m.guard, end, 0)
            self.visit_stmts(m.body)
        self.use_label(end)

    def match(self, s):
        pc = PatternContext()
        pc.fail_pop = None
        self.match_inner(s, pc)


class _ConstSet:
    """A set with PySet semantics for constant keys (1 == 1.0 == True)."""

    def __init__(self):
        self._s = set()

    def __contains__(self, v):
        return v in self._s

    def add(self, v):
        self._s.add(v)


def compile_ast(mod, filename, flags, optimize):
    """_PyAST_Compile."""
    c = Compiler(mod, filename, flags, optimize)
    return c.compile_mod(mod)
