"""Port of Python/symtable.c: symbol table construction and scope analysis."""

from . import pyast as ast
from .errors import SourceLocation, raise_syntax_error_at
from .future import get_docstring, CO_FUTURE_ANNOTATIONS

# Block types
FunctionBlock = 0
ClassBlock = 1
ModuleBlock = 2
AnnotationBlock = 3
TypeAliasBlock = 4
TypeParametersBlock = 5
TypeVariableBlock = 6

# Comprehension types
NoComprehension = 0
ListComprehension = 1
DictComprehension = 2
SetComprehension = 3
GeneratorExpression = 4

# Flags for def-use information
DEF_GLOBAL = 1
DEF_LOCAL = 2
DEF_PARAM = 2 << 1
DEF_NONLOCAL = 2 << 2
USE = 2 << 3
DEF_FREE_CLASS = 2 << 5
DEF_IMPORT = 2 << 6
DEF_ANNOT = 2 << 7
DEF_COMP_ITER = 2 << 8
DEF_TYPE_PARAM = 2 << 9
DEF_COMP_CELL = 2 << 10

DEF_BOUND = DEF_LOCAL | DEF_PARAM | DEF_IMPORT

SCOPE_OFFSET = 12
SCOPE_MASK = DEF_GLOBAL | DEF_LOCAL | DEF_PARAM | DEF_NONLOCAL

LOCAL = 1
GLOBAL_EXPLICIT = 2
GLOBAL_IMPLICIT = 3
FREE = 4
CELL = 5

PyCF_ALLOW_TOP_LEVEL_AWAIT = 0x2000

GLOBAL_PARAM = "name '%s' is parameter and global"
NONLOCAL_PARAM = "name '%s' is parameter and nonlocal"
GLOBAL_AFTER_ASSIGN = "name '%s' is assigned to before global declaration"
NONLOCAL_AFTER_ASSIGN = "name '%s' is assigned to before nonlocal declaration"
GLOBAL_AFTER_USE = "name '%s' is used prior to global declaration"
NONLOCAL_AFTER_USE = "name '%s' is used prior to nonlocal declaration"
GLOBAL_ANNOT = "annotated name '%s' can't be global"
NONLOCAL_ANNOT = "annotated name '%s' can't be nonlocal"
IMPORT_STAR_WARNING = "import * only allowed at module level"
NAMED_EXPR_COMP_IN_CLASS = \
    "assignment expression within a comprehension cannot be used in a class body"
NAMED_EXPR_COMP_IN_TYPEVAR_BOUND = \
    "assignment expression within a comprehension cannot be used in a TypeVar bound"
NAMED_EXPR_COMP_IN_TYPEALIAS = \
    "assignment expression within a comprehension cannot be used in a type alias"
NAMED_EXPR_COMP_IN_TYPEPARAM = \
    "assignment expression within a comprehension cannot be used within the definition of a generic"
NAMED_EXPR_COMP_CONFLICT = \
    "assignment expression cannot rebind comprehension iteration variable '%s'"
NAMED_EXPR_COMP_INNER_LOOP_CONFLICT = \
    "comprehension inner loop cannot rebind assignment expression target '%s'"
NAMED_EXPR_COMP_ITER_EXPR = \
    "assignment expression cannot be used in a comprehension iterable expression"
ANNOTATION_NOT_ALLOWED = "%s cannot be used within an annotation"
EXPR_NOT_ALLOWED_IN_TYPE_VARIABLE = "%s cannot be used within %s"
EXPR_NOT_ALLOWED_IN_TYPE_ALIAS = "%s cannot be used within a type alias"
EXPR_NOT_ALLOWED_IN_TYPE_PARAMETERS = "%s cannot be used within the definition of a generic"
DUPLICATE_TYPE_PARAM = "duplicate type parameter '%s'"
ASYNC_WITH_OUTSIDE_ASYNC_FUNC = "'async with' outside async function"
ASYNC_FOR_OUTSIDE_ASYNC_FUNC = "'async for' outside async function"
DUPLICATE_ARGUMENT = "duplicate argument '%s' in function definition"


def symbol_to_scope(s):
    return (s >> SCOPE_OFFSET) & SCOPE_MASK


def LOCATION(n):
    return SourceLocation(n.lineno, n.end_lineno, n.col_offset, n.end_col_offset)


# Keys used to find symbol table entries from AST nodes (the C code uses
# node addresses; we use identity based keys).

def key_of(node):
    return id(node)


def key_plus1(node):
    """(uintptr_t)node + 1"""
    return ('+1', id(node))


def mangle(private, ident):
    """_Py_Mangle."""
    if private is None or not isinstance(private, str) or not ident.startswith('__'):
        return ident
    nlen = len(ident)
    if (ident.endswith('__')) or ('.' in ident):
        return ident
    ipriv = 0
    plen = len(private)
    while ipriv < plen and private[ipriv] == '_':
        ipriv += 1
    if ipriv == plen:
        return ident
    return '_' + private[ipriv:] + ident


def maybe_mangle(private, ste, name):
    """_Py_MaybeMangle."""
    if ste.mangled_names is not None:
        if name not in ste.mangled_names:
            return name
    return mangle(private, name)


class STEntry:
    """PySTEntryObject."""

    def __init__(self, st, name, block, key, loc):
        self.table = st
        self.id = key
        self.name = name
        self.symbols = {}
        self.varnames = []
        self.children = []
        self.directives = None
        self.mangled_names = None
        self.type = block
        self.scope_info = None
        self.nested = 0
        self.varargs = 0
        self.varkeywords = 0
        self.annotations_used = 0
        self.loc = loc
        if st.cur is not None and (st.cur.nested or is_function_like(st.cur)):
            self.nested = 1
        self.generator = 0
        self.coroutine = 0
        self.comprehension = NoComprehension
        self.returns_value = 0
        self.needs_class_closure = 0
        self.comp_inlined = 0
        self.comp_iter_target = 0
        self.can_see_class_scope = 0
        self.comp_iter_expr = 0
        self.needs_classdict = 0
        self.has_conditional_annotations = 0
        self.in_conditional_block = 0
        self.in_unevaluated_annotation = 0
        self.annotation_block = None
        self.has_docstring = 0
        self.method = 0
        if st.cur is not None and st.cur.type == ClassBlock and block == FunctionBlock:
            self.method = 1
        st.blocks[key] = self

    def __repr__(self):
        return "<symtable entry %s(%r), line %d>" % (self.name, self.id, self.loc.lineno)


def is_function_like(ste):
    return ste.type in (FunctionBlock, AnnotationBlock, TypeVariableBlock,
                        TypeAliasBlock, TypeParametersBlock)


def get_symbol(ste, name):
    return ste.symbols.get(name, 0)


def get_scope(ste, name):
    return symbol_to_scope(get_symbol(ste, name))


class SymtableError(Exception):
    pass


class SymTable:
    """struct symtable."""

    def __init__(self, filename, future):
        self.filename = filename
        self.blocks = {}
        self.stack = []
        self.cur = None
        self.top = None
        self.private = None
        self.future = future
        self.global_ = None

    # -- errors ------------------------------------------------------

    def _error(self, msg, loc):
        raise_syntax_error_at(msg, self.filename, loc)

    def lookup_entry_obj(self, key):
        return self.blocks[key]

    # -- analysis pass (second pass) ------------------------------------

    def _error_at_directive(self, ste, name, msg):
        assert ste.directives is not None
        for data in ste.directives:
            if data[0] == name:
                raise_syntax_error_at(msg, self.filename,
                                      SourceLocation(data[1], data[3], data[2], data[4]))
        raise RuntimeError("BUG: internal directive bookkeeping broken")

    def _analyze_name(self, ste, scopes, name, flags, bound, local, free, global_,
                      type_params, class_entry):
        if flags & DEF_GLOBAL:
            if flags & DEF_NONLOCAL:
                self._error_at_directive(ste, name,
                                         "name '%s' is nonlocal and global" % name)
            scopes[name] = GLOBAL_EXPLICIT
            global_.add(name)
            if bound is not None:
                bound.discard(name)
            return
        if flags & DEF_NONLOCAL:
            if bound is None:
                self._error_at_directive(ste, name,
                                         "nonlocal declaration not allowed at module level")
            if name not in bound:
                self._error_at_directive(ste, name,
                                         "no binding for nonlocal '%s' found" % name)
            if name in type_params:
                self._error_at_directive(
                    ste, name, "nonlocal binding not allowed for type parameter '%s'" % name)
            scopes[name] = FREE
            free.add(name)
            return
        if flags & DEF_BOUND:
            scopes[name] = LOCAL
            local.add(name)
            global_.discard(name)
            if flags & DEF_TYPE_PARAM:
                type_params.add(name)
            else:
                type_params.discard(name)
            return
        if class_entry is not None:
            class_flags = get_symbol(class_entry, name)
            if class_flags & DEF_GLOBAL:
                scopes[name] = GLOBAL_EXPLICIT
                return
            elif (class_flags & DEF_BOUND) and not (class_flags & DEF_NONLOCAL):
                scopes[name] = GLOBAL_IMPLICIT
                return
        if bound is not None:
            if name in bound:
                scopes[name] = FREE
                free.add(name)
                return
        if global_ is not None:
            if name in global_:
                scopes[name] = GLOBAL_IMPLICIT
                return
        scopes[name] = GLOBAL_IMPLICIT

    @staticmethod
    def _is_free_in_any_child(entry, key):
        for child_ste in entry.children:
            if get_scope(child_ste, key) == FREE:
                return 1
        return 0

    def _inline_comprehension(self, ste, comp, scopes, comp_free, inlined_cells):
        remove_dunder_class = 0
        remove_dunder_classdict = 0
        remove_dunder_cond_annotations = 0
        for k, comp_flags in list(comp.symbols.items()):
            if comp_flags & DEF_PARAM:
                assert k == ".0"
                continue
            scope = symbol_to_scope(comp_flags)
            only_flags = comp_flags & ((1 << SCOPE_OFFSET) - 1)
            if scope == CELL or only_flags & DEF_COMP_CELL:
                inlined_cells.add(k)
            existing = ste.symbols.get(k)
            if (scope == FREE and ste.type == ClassBlock
                    and k in ("__class__", "__classdict__", "__conditional_annotations__")):
                scope = GLOBAL_IMPLICIT
                child_needs_free = self._is_free_in_any_child(comp, k)
                if not child_needs_free:
                    comp_free.discard(k)
                if k == "__class__":
                    remove_dunder_class = 1
                elif k == "__conditional_annotations__":
                    remove_dunder_cond_annotations = 1
                else:
                    remove_dunder_classdict = 1
            if existing is None:
                ste.symbols[k] = only_flags
                scopes[k] = scope
            else:
                flags = existing
                if (flags & DEF_BOUND) and ste.type != ClassBlock:
                    ok = self._is_free_in_any_child(comp, k)
                    if not ok:
                        comp_free.discard(k)
        if remove_dunder_class:
            del comp.symbols["__class__"]
        if remove_dunder_classdict:
            del comp.symbols["__classdict__"]
        if remove_dunder_cond_annotations:
            del comp.symbols["__conditional_annotations__"]

    @staticmethod
    def _analyze_cells(scopes, free, inlined_cells):
        for name, scope in list(scopes.items()):
            if scope != LOCAL:
                continue
            if name not in free:
                if name not in inlined_cells:
                    continue
            scopes[name] = CELL
            free.discard(name)

    @staticmethod
    def _drop_class_free(ste, free):
        if "__class__" in free:
            free.discard("__class__")
            ste.needs_class_closure = 1
        if "__classdict__" in free:
            free.discard("__classdict__")
            ste.needs_classdict = 1
        if "__conditional_annotations__" in free:
            free.discard("__conditional_annotations__")
            ste.has_conditional_annotations = 1

    @staticmethod
    def _update_symbols(symbols, scopes, bound, free, inlined_cells, classflag):
        for name in list(symbols):
            flags = symbols[name]
            if name in inlined_cells:
                flags |= DEF_COMP_CELL
            if name not in scopes:
                raise KeyError(name)
            scope = scopes[name]
            flags |= (scope << SCOPE_OFFSET)
            symbols[name] = flags
        v_free = FREE << SCOPE_OFFSET
        for name in free:
            v = symbols.get(name)
            if v is not None:
                if classflag:
                    symbols[name] = v | DEF_FREE_CLASS
                continue
            if bound is not None:
                if name not in bound:
                    continue
            symbols[name] = v_free

    def _analyze_block(self, ste, bound, free, global_, type_params, class_entry):
        local = set()
        scopes = {}
        newglobal = set()
        newfree = set()
        newbound = set()
        inlined_cells = set()
        if ste.type == ClassBlock:
            newglobal |= global_
            if bound is not None:
                newbound |= bound
        for name, flags in list(ste.symbols.items()):
            self._analyze_name(ste, scopes, name, flags, bound, local, free, global_,
                               type_params, class_entry)
        if ste.type != ClassBlock:
            if is_function_like(ste):
                newbound |= local
            if bound is not None:
                newbound |= bound
            newglobal |= global_
        else:
            newbound.add("__class__")
            newbound.add("__classdict__")
            newbound.add("__conditional_annotations__")
        for entry in list(ste.children):
            new_class_entry = None
            if entry.can_see_class_scope:
                if ste.type == ClassBlock:
                    new_class_entry = ste
                elif class_entry is not None:
                    new_class_entry = class_entry
            inline_comp = (entry.comprehension and not entry.generator
                           and not ste.can_see_class_scope)
            child_free = self._analyze_child_block(entry, newbound, newfree, newglobal,
                                                   type_params, new_class_entry)
            if inline_comp:
                self._inline_comprehension(ste, entry, scopes, child_free, inlined_cells)
                entry.comp_inlined = 1
            newfree |= child_free
        i = len(ste.children) - 1
        while i >= 0:
            entry = ste.children[i]
            if entry.comp_inlined:
                ste.children[i:i + 1] = entry.children
            i -= 1
        if is_function_like(ste):
            self._analyze_cells(scopes, newfree, inlined_cells)
        elif ste.type == ClassBlock:
            self._drop_class_free(ste, newfree)
        self._update_symbols(ste.symbols, scopes, bound, newfree, inlined_cells,
                             (ste.type == ClassBlock) or ste.can_see_class_scope)
        free |= newfree

    def _analyze_child_block(self, entry, bound, free, global_, type_params, class_entry):
        temp_bound = set(bound)
        temp_free = set(free)
        temp_global = set(global_)
        temp_type_params = set(type_params)
        self._analyze_block(entry, temp_bound, temp_free, temp_global, temp_type_params,
                            class_entry)
        return temp_free

    def analyze(self):
        free = set()
        global_ = set()
        type_params = set()
        self._analyze_block(self.top, None, free, global_, type_params, None)

    # -- first pass ------------------------------------------------------

    def exit_block(self):
        self.cur = None
        if self.stack:
            self.stack.pop()
            if self.stack:
                self.cur = self.stack[-1]

    def enter_existing_block(self, ste, add_to_children):
        self.stack.append(ste)
        prev = self.cur
        if prev is not None:
            ste.comp_iter_expr = prev.comp_iter_expr
        if prev is not None and prev.mangled_names is not None and ste.type != ClassBlock:
            ste.mangled_names = prev.mangled_names
        self.cur = ste
        if (self.future.ff_features & CO_FUTURE_ANNOTATIONS) and ste.type == AnnotationBlock:
            return
        if ste.type == ModuleBlock:
            self.global_ = self.cur.symbols
        if add_to_children and prev is not None:
            prev.children.append(ste)

    def enter_block(self, name, block, key, loc):
        ste = STEntry(self, name, block, key, loc)
        self.enter_existing_block(ste, True)
        if block in (AnnotationBlock, TypeVariableBlock, TypeAliasBlock):
            self.add_def(".format", DEF_PARAM, loc)
            self.add_def(".format", USE, loc)

    def lookup_entry(self, ste, name):
        mangled = maybe_mangle(self.private, ste, name)
        return get_symbol(ste, mangled)

    def lookup(self, name):
        return self.lookup_entry(self.cur, name)

    def add_def_helper(self, name, flag, ste, loc):
        mangled = maybe_mangle(self.private, self.cur, name)
        d = ste.symbols
        if mangled in d:
            val = d[mangled]
            if (flag & DEF_PARAM) and (val & DEF_PARAM):
                self._error(DUPLICATE_ARGUMENT % name, loc)
            if (flag & DEF_TYPE_PARAM) and (val & DEF_TYPE_PARAM):
                self._error(DUPLICATE_TYPE_PARAM % name, loc)
            val |= flag
        else:
            val = flag
        if ste.comp_iter_target:
            if val & (DEF_GLOBAL | DEF_NONLOCAL):
                self._error(NAMED_EXPR_COMP_INNER_LOOP_CONFLICT % name, loc)
            val |= DEF_COMP_ITER
        d[mangled] = val
        if flag & DEF_PARAM:
            ste.varnames.append(mangled)
        elif flag & DEF_GLOBAL:
            val = self.global_.get(mangled, 0)
            val |= flag
            self.global_[mangled] = val

    def check_name(self, name, loc, ctx):
        if ctx is ast.Store_singleton and name == "__debug__":
            self._error("cannot assign to __debug__", loc)
        if ctx is ast.Del_singleton and name == "__debug__":
            self._error("cannot delete __debug__", loc)

    def check_keywords(self, keywords):
        for key in keywords:
            if key.arg is not None:
                self.check_name(key.arg, LOCATION(key), ast.Store_singleton)

    def check_kwd_patterns(self, p):
        for attr, pat in zip(p.kwd_attrs, p.kwd_patterns):
            self.check_name(attr, LOCATION(pat), ast.Store_singleton)

    def add_def_ctx(self, name, flag, loc, ctx):
        write_mask = DEF_PARAM | DEF_LOCAL | DEF_IMPORT
        if flag & write_mask:
            self.check_name(name, loc, ctx)
        if (flag & DEF_TYPE_PARAM) and self.cur.mangled_names is not None:
            self.cur.mangled_names.add(name)
        self.add_def_helper(name, flag, self.cur, loc)

    def add_def(self, name, flag, loc):
        self.add_def_ctx(name, flag, loc,
                         ast.Load_singleton if flag == USE else ast.Store_singleton)

    def enter_type_param_block(self, name, key, has_defaults, has_kwdefaults, is_class, loc):
        current_type = self.cur.type
        self.enter_block(name, TypeParametersBlock, key, loc)
        if current_type == ClassBlock:
            self.cur.can_see_class_scope = 1
            self.add_def("__classdict__", USE, loc)
        if is_class:
            self.add_def(".type_params", DEF_LOCAL, loc)
            self.add_def(".type_params", USE, loc)
            self.add_def(".generic_base", DEF_LOCAL, loc)
            self.add_def(".generic_base", USE, loc)
        if has_defaults:
            self.add_def(".defaults", DEF_PARAM, loc)
        if has_kwdefaults:
            self.add_def(".kwdefaults", DEF_PARAM, loc)

    def record_directive(self, name, loc):
        if self.cur.directives is None:
            self.cur.directives = []
        mangled = maybe_mangle(self.private, self.cur, name)
        self.cur.directives.append((mangled, loc.lineno, loc.col_offset,
                                    loc.end_lineno, loc.end_col_offset))

    @staticmethod
    def has_kwonlydefaults(kwonlyargs, kw_defaults):
        for i in range(len(kwonlyargs)):
            if kw_defaults[i] is not None:
                return 1
        return 0

    def check_import_from(self, s):
        fut = self.future.ff_location
        if (s.module is not None and s.level == 0 and s.module == "__future__"
                and ((s.lineno > fut.lineno)
                     or ((s.lineno == fut.end_lineno) and (s.col_offset > fut.end_col_offset)))):
            self._error("from __future__ imports must occur at the beginning of the file",
                        LOCATION(s))

    def allows_top_level_await(self):
        return ((self.future.ff_features & PyCF_ALLOW_TOP_LEVEL_AWAIT)
                and self.cur.type == ModuleBlock)

    def maybe_set_ste_coroutine_for_module(self):
        if self.allows_top_level_await():
            self.cur.coroutine = 1

    def is_async_def(self):
        return self.cur.type == FunctionBlock and self.cur.coroutine

    # -- visitors --------------------------------------------------------

    def visit_stmts(self, seq):
        for s in seq:
            self.visit_stmt(s)

    def visit_exprs(self, seq):
        for e in seq:
            self.visit_expr(e)

    def visit_exprs_with_null(self, seq):
        for e in seq:
            if e is None:
                continue
            self.visit_expr(e)

    def _visit_funcdef(self, s, is_async):
        self.add_def(s.name, DEF_LOCAL, LOCATION(s))
        if s.args.defaults:
            self.visit_exprs(s.args.defaults)
        if s.args.kw_defaults:
            self.visit_exprs_with_null(s.args.kw_defaults)
        if s.decorator_list:
            self.visit_exprs(s.decorator_list)
        if len(s.type_params) > 0:
            self.enter_type_param_block(
                s.name, key_of(s.type_params), s.args.defaults is not None,
                self.has_kwonlydefaults(s.args.kwonlyargs, s.args.kw_defaults),
                False, LOCATION(s))
            for tp in s.type_params:
                self.visit_type_param(tp)
        new_ste = STEntry(self, s.name, FunctionBlock, key_of(s), LOCATION(s))
        if get_docstring(s.body) is not None:
            new_ste.has_docstring = 1
        self.visit_annotations(s, s.args, s.returns, new_ste)
        self.enter_existing_block(new_ste, True)
        if is_async:
            self.cur.coroutine = 1
        self.visit_arguments(s.args)
        self.visit_stmts(s.body)
        self.exit_block()
        if len(s.type_params) > 0:
            self.exit_block()

    def visit_stmt(self, s):
        k = type(s)
        if k is ast.FunctionDef:
            self._visit_funcdef(s, False)
        elif k is ast.AsyncFunctionDef:
            self._visit_funcdef(s, True)
        elif k is ast.ClassDef:
            self.add_def(s.name, DEF_LOCAL, LOCATION(s))
            if s.decorator_list:
                self.visit_exprs(s.decorator_list)
            tmp = self.private
            if len(s.type_params) > 0:
                self.enter_type_param_block(s.name, key_of(s.type_params), False, False,
                                            True, LOCATION(s))
                self.private = s.name
                self.cur.mangled_names = set()
                for tp in s.type_params:
                    self.visit_type_param(tp)
            self.visit_exprs(s.bases)
            self.check_keywords(s.keywords)
            for kw in s.keywords:
                self.visit_keyword(kw)
            self.enter_block(s.name, ClassBlock, key_of(s), LOCATION(s))
            self.private = s.name
            if len(s.type_params) > 0:
                self.add_def("__type_params__", DEF_LOCAL, LOCATION(s))
                self.add_def(".type_params", USE, LOCATION(s))
            if get_docstring(s.body) is not None:
                self.cur.has_docstring = 1
            self.visit_stmts(s.body)
            self.exit_block()
            if len(s.type_params) > 0:
                self.exit_block()
            self.private = tmp
        elif k is ast.TypeAlias:
            self.visit_expr(s.name)
            name = s.name.id
            is_in_class = self.cur.type == ClassBlock
            is_generic = len(s.type_params) > 0
            if is_generic:
                self.enter_type_param_block(name, key_of(s.type_params), False, False,
                                            False, LOCATION(s))
                for tp in s.type_params:
                    self.visit_type_param(tp)
            self.enter_block(name, TypeAliasBlock, key_of(s), LOCATION(s))
            self.cur.can_see_class_scope = is_in_class
            if is_in_class:
                self.add_def("__classdict__", USE, LOCATION(s.value))
            self.visit_expr(s.value)
            self.exit_block()
            if is_generic:
                self.exit_block()
        elif k is ast.Return:
            if s.value is not None:
                self.visit_expr(s.value)
                self.cur.returns_value = 1
        elif k is ast.Delete:
            self.visit_exprs(s.targets)
        elif k is ast.Assign:
            self.visit_exprs(s.targets)
            self.visit_expr(s.value)
        elif k is ast.AnnAssign:
            self.cur.annotations_used = 1
            if isinstance(s.target, ast.Name):
                e_name = s.target
                cur = self.lookup(e_name.id)
                if ((cur & (DEF_GLOBAL | DEF_NONLOCAL))
                        and (self.cur.symbols is not self.global_) and s.simple):
                    self._error((GLOBAL_ANNOT if cur & DEF_GLOBAL else NONLOCAL_ANNOT)
                                % e_name.id, LOCATION(s))
                if s.simple:
                    self.add_def(e_name.id, DEF_ANNOT | DEF_LOCAL, LOCATION(e_name))
                else:
                    if s.value is not None:
                        self.add_def(e_name.id, DEF_LOCAL, LOCATION(e_name))
            else:
                self.visit_expr(s.target)
            self.visit_annotation(s.annotation, ('annassign', self.cur.id))
            if s.value is not None:
                self.visit_expr(s.value)
        elif k is ast.AugAssign:
            self.visit_expr(s.target)
            self.visit_expr(s.value)
        elif k is ast.For or k is ast.AsyncFor:
            if k is ast.AsyncFor:
                self.maybe_set_ste_coroutine_for_module()
                self.raise_if_not_coroutine(ASYNC_FOR_OUTSIDE_ASYNC_FUNC, LOCATION(s))
            self.visit_expr(s.target)
            self.visit_expr(s.iter)
            in_cond = self.cur.in_conditional_block
            self.cur.in_conditional_block = 1
            self.visit_stmts(s.body)
            if s.orelse:
                self.visit_stmts(s.orelse)
            self.cur.in_conditional_block = in_cond
        elif k is ast.While:
            self.visit_expr(s.test)
            in_cond = self.cur.in_conditional_block
            self.cur.in_conditional_block = 1
            self.visit_stmts(s.body)
            if s.orelse:
                self.visit_stmts(s.orelse)
            self.cur.in_conditional_block = in_cond
        elif k is ast.If:
            self.visit_expr(s.test)
            in_cond = self.cur.in_conditional_block
            self.cur.in_conditional_block = 1
            self.visit_stmts(s.body)
            if s.orelse:
                self.visit_stmts(s.orelse)
            self.cur.in_conditional_block = in_cond
        elif k is ast.Match:
            self.visit_expr(s.subject)
            in_cond = self.cur.in_conditional_block
            self.cur.in_conditional_block = 1
            for m in s.cases:
                self.visit_match_case(m)
            self.cur.in_conditional_block = in_cond
        elif k is ast.Raise:
            if s.exc is not None:
                self.visit_expr(s.exc)
                if s.cause is not None:
                    self.visit_expr(s.cause)
        elif k is ast.Try or k is ast.TryStar:
            in_cond = self.cur.in_conditional_block
            self.cur.in_conditional_block = 1
            self.visit_stmts(s.body)
            for eh in s.handlers:
                self.visit_excepthandler(eh)
            self.visit_stmts(s.orelse)
            self.visit_stmts(s.finalbody)
            self.cur.in_conditional_block = in_cond
        elif k is ast.Assert:
            self.visit_expr(s.test)
            if s.msg is not None:
                self.visit_expr(s.msg)
        elif k is ast.Import:
            for a in s.names:
                self.visit_alias(a)
        elif k is ast.ImportFrom:
            for a in s.names:
                self.visit_alias(a)
            self.check_import_from(s)
        elif k is ast.Global:
            for name in s.names:
                cur = self.lookup(name)
                if cur & (DEF_PARAM | DEF_LOCAL | USE | DEF_ANNOT):
                    if cur & DEF_PARAM:
                        msg = GLOBAL_PARAM
                    elif cur & USE:
                        msg = GLOBAL_AFTER_USE
                    elif cur & DEF_ANNOT:
                        msg = GLOBAL_ANNOT
                    else:
                        msg = GLOBAL_AFTER_ASSIGN
                    self._error(msg % name, LOCATION(s))
                self.add_def(name, DEF_GLOBAL, LOCATION(s))
                self.record_directive(name, LOCATION(s))
        elif k is ast.Nonlocal:
            for name in s.names:
                cur = self.lookup(name)
                if cur & (DEF_PARAM | DEF_LOCAL | USE | DEF_ANNOT):
                    if cur & DEF_PARAM:
                        msg = NONLOCAL_PARAM
                    elif cur & USE:
                        msg = NONLOCAL_AFTER_USE
                    elif cur & DEF_ANNOT:
                        msg = NONLOCAL_ANNOT
                    else:
                        msg = NONLOCAL_AFTER_ASSIGN
                    self._error(msg % name, LOCATION(s))
                self.add_def(name, DEF_NONLOCAL, LOCATION(s))
                self.record_directive(name, LOCATION(s))
        elif k is ast.Expr:
            self.visit_expr(s.value)
        elif k in (ast.Pass, ast.Break, ast.Continue):
            pass
        elif k is ast.With or k is ast.AsyncWith:
            if k is ast.AsyncWith:
                self.maybe_set_ste_coroutine_for_module()
                self.raise_if_not_coroutine(ASYNC_WITH_OUTSIDE_ASYNC_FUNC, LOCATION(s))
            in_cond = self.cur.in_conditional_block
            self.cur.in_conditional_block = 1
            for item in s.items:
                self.visit_withitem(item)
            self.visit_stmts(s.body)
            self.cur.in_conditional_block = in_cond
        else:
            raise SystemError("unknown statement %r" % k)

    def extend_namedexpr_scope(self, e):
        target_name = e.id
        for i in range(len(self.stack) - 1, -1, -1):
            ste = self.stack[i]
            if ste.comprehension:
                target_in_scope = self.lookup_entry(ste, target_name)
                if (target_in_scope & DEF_COMP_ITER) and (target_in_scope & DEF_LOCAL):
                    self._error(NAMED_EXPR_COMP_CONFLICT % target_name, LOCATION(e))
                continue
            if ste.type == FunctionBlock:
                target_in_scope = self.lookup_entry(ste, target_name)
                if target_in_scope & DEF_GLOBAL:
                    self.add_def(target_name, DEF_GLOBAL, LOCATION(e))
                else:
                    self.add_def(target_name, DEF_NONLOCAL, LOCATION(e))
                self.record_directive(target_name, LOCATION(e))
                self.add_def_helper(target_name, DEF_LOCAL, ste, LOCATION(e))
                return
            if ste.type == ModuleBlock:
                self.add_def(target_name, DEF_GLOBAL, LOCATION(e))
                self.record_directive(target_name, LOCATION(e))
                self.add_def_helper(target_name, DEF_GLOBAL, ste, LOCATION(e))
                return
            if ste.type in (ClassBlock, TypeParametersBlock, TypeAliasBlock, TypeVariableBlock):
                if ste.type == ClassBlock:
                    msg = NAMED_EXPR_COMP_IN_CLASS
                elif ste.type == TypeParametersBlock:
                    msg = NAMED_EXPR_COMP_IN_TYPEPARAM
                elif ste.type == TypeAliasBlock:
                    msg = NAMED_EXPR_COMP_IN_TYPEALIAS
                else:
                    msg = NAMED_EXPR_COMP_IN_TYPEVAR_BOUND
                self._error(msg, LOCATION(e))
        raise SystemError("unreachable in extend_namedexpr_scope")

    def handle_namedexpr(self, e):
        if self.cur.comp_iter_expr > 0:
            self._error(NAMED_EXPR_COMP_ITER_EXPR, LOCATION(e))
        if self.cur.comprehension:
            self.extend_namedexpr_scope(e.target)
        self.visit_expr(e.value)
        self.visit_expr(e.target)

    def visit_expr(self, e):
        k = type(e)
        if k is ast.NamedExpr:
            self.raise_if_annotation_block("named expression", e)
            self.handle_namedexpr(e)
        elif k is ast.BoolOp:
            self.visit_exprs(e.values)
        elif k is ast.BinOp:
            self.visit_expr(e.left)
            self.visit_expr(e.right)
        elif k is ast.UnaryOp:
            self.visit_expr(e.operand)
        elif k is ast.Lambda:
            if e.args.defaults:
                self.visit_exprs(e.args.defaults)
            if e.args.kw_defaults:
                self.visit_exprs_with_null(e.args.kw_defaults)
            self.enter_block("lambda", FunctionBlock, key_of(e), LOCATION(e))
            self.visit_arguments(e.args)
            self.visit_expr(e.body)
            self.exit_block()
        elif k is ast.IfExp:
            self.visit_expr(e.test)
            self.visit_expr(e.body)
            self.visit_expr(e.orelse)
        elif k is ast.Dict:
            self.visit_exprs_with_null(e.keys)
            self.visit_exprs(e.values)
        elif k is ast.Set:
            self.visit_exprs(e.elts)
        elif k is ast.GeneratorExp:
            self.handle_comprehension(e, "genexpr", e.generators, e.elt, None)
        elif k is ast.ListComp:
            self.handle_comprehension(e, "listcomp", e.generators, e.elt, None)
        elif k is ast.SetComp:
            self.handle_comprehension(e, "setcomp", e.generators, e.elt, None)
        elif k is ast.DictComp:
            self.handle_comprehension(e, "dictcomp", e.generators, e.key, e.value)
        elif k is ast.Yield:
            self.raise_if_annotation_block("yield expression", e)
            if e.value is not None:
                self.visit_expr(e.value)
            self.cur.generator = 1
            if self.cur.comprehension:
                self.raise_if_comprehension_block(e)
        elif k is ast.YieldFrom:
            self.raise_if_annotation_block("yield expression", e)
            self.visit_expr(e.value)
            self.cur.generator = 1
            if self.cur.comprehension:
                self.raise_if_comprehension_block(e)
        elif k is ast.Await:
            self.raise_if_annotation_block("await expression", e)
            if not self.allows_top_level_await():
                if not is_function_like(self.cur):
                    self._error("'await' outside function", LOCATION(e))
                if not self.is_async_def() and self.cur.comprehension == NoComprehension:
                    self._error("'await' outside async function", LOCATION(e))
            self.visit_expr(e.value)
            self.cur.coroutine = 1
        elif k is ast.Compare:
            self.visit_expr(e.left)
            self.visit_exprs(e.comparators)
        elif k is ast.Call:
            self.visit_expr(e.func)
            self.visit_exprs(e.args)
            self.check_keywords(e.keywords)
            for kw in e.keywords:
                if kw is None:
                    continue
                self.visit_keyword(kw)
        elif k is ast.FormattedValue:
            self.visit_expr(e.value)
            if e.format_spec is not None:
                self.visit_expr(e.format_spec)
        elif k is ast.Interpolation:
            self.visit_expr(e.value)
            if e.format_spec is not None:
                self.visit_expr(e.format_spec)
        elif k is ast.JoinedStr:
            self.visit_exprs(e.values)
        elif k is ast.TemplateStr:
            self.visit_exprs(e.values)
        elif k is ast.Constant:
            pass
        elif k is ast.Attribute:
            self.check_name(e.attr, LOCATION(e), e.ctx)
            self.visit_expr(e.value)
        elif k is ast.Subscript:
            self.visit_expr(e.value)
            self.visit_expr(e.slice)
        elif k is ast.Starred:
            self.visit_expr(e.value)
        elif k is ast.Slice:
            if e.lower is not None:
                self.visit_expr(e.lower)
            if e.upper is not None:
                self.visit_expr(e.upper)
            if e.step is not None:
                self.visit_expr(e.step)
        elif k is ast.Name:
            if not self.cur.in_unevaluated_annotation:
                self.add_def_ctx(e.id, USE if e.ctx is ast.Load_singleton else DEF_LOCAL,
                                 LOCATION(e), e.ctx)
                if (e.ctx is ast.Load_singleton and is_function_like(self.cur)
                        and e.id == "super"):
                    self.add_def("__class__", USE, LOCATION(e))
        elif k is ast.List:
            self.visit_exprs(e.elts)
        elif k is ast.Tuple:
            self.visit_exprs(e.elts)
        else:
            raise SystemError("unknown expression %r" % k)

    def visit_type_param_bound_or_default(self, e, name, key, loc_node, scope_info):
        if name == "__classdict__":
            self._error("reserved name '%s' cannot be used for type parameter" % name,
                        LOCATION(loc_node))
        if e is not None:
            is_in_class = self.cur.can_see_class_scope
            self.enter_block(name, TypeVariableBlock, key, LOCATION(e))
            self.cur.can_see_class_scope = is_in_class
            if is_in_class:
                self.add_def("__classdict__", USE, LOCATION(e))
            self.cur.scope_info = scope_info
            self.visit_expr(e)
            self.exit_block()

    def visit_type_param(self, tp):
        k = type(tp)
        if k is ast.TypeVar:
            self.add_def(tp.name, DEF_TYPE_PARAM | DEF_LOCAL, LOCATION(tp))
            scope_info = None
            bound = tp.bound
            if bound is not None:
                scope_info = ("a TypeVar constraint" if isinstance(bound, ast.Tuple)
                              else "a TypeVar bound")
            self.visit_type_param_bound_or_default(tp.bound, tp.name, key_of(tp), tp,
                                                   scope_info)
            self.visit_type_param_bound_or_default(tp.default_value, tp.name,
                                                   key_plus1(tp), tp, "a TypeVar default")
        elif k is ast.TypeVarTuple:
            self.add_def(tp.name, DEF_TYPE_PARAM | DEF_LOCAL, LOCATION(tp))
            self.visit_type_param_bound_or_default(tp.default_value, tp.name, key_of(tp),
                                                   tp, "a TypeVarTuple default")
        elif k is ast.ParamSpec:
            self.add_def(tp.name, DEF_TYPE_PARAM | DEF_LOCAL, LOCATION(tp))
            self.visit_type_param_bound_or_default(tp.default_value, tp.name, key_of(tp),
                                                   tp, "a ParamSpec default")

    def visit_pattern(self, p):
        k = type(p)
        if k is ast.MatchValue:
            self.visit_expr(p.value)
        elif k is ast.MatchSingleton:
            pass
        elif k is ast.MatchSequence:
            for sub in p.patterns:
                self.visit_pattern(sub)
        elif k is ast.MatchStar:
            if p.name is not None:
                self.add_def(p.name, DEF_LOCAL, LOCATION(p))
        elif k is ast.MatchMapping:
            self.visit_exprs(p.keys)
            for sub in p.patterns:
                self.visit_pattern(sub)
            if p.rest is not None:
                self.add_def(p.rest, DEF_LOCAL, LOCATION(p))
        elif k is ast.MatchClass:
            self.visit_expr(p.cls)
            for sub in p.patterns:
                self.visit_pattern(sub)
            self.check_kwd_patterns(p)
            for sub in p.kwd_patterns:
                self.visit_pattern(sub)
        elif k is ast.MatchAs:
            if p.pattern is not None:
                self.visit_pattern(p.pattern)
            if p.name is not None:
                self.add_def(p.name, DEF_LOCAL, LOCATION(p))
        elif k is ast.MatchOr:
            for sub in p.patterns:
                self.visit_pattern(sub)

    def implicit_arg(self, pos):
        self.add_def(".%d" % pos, DEF_PARAM, self.cur.loc)

    def visit_params(self, args):
        for arg in args:
            self.add_def(arg.arg, DEF_PARAM, LOCATION(arg))

    def visit_annotation(self, annotation, key):
        is_unevaluated = self.cur.type == FunctionBlock
        if (((self.cur.type == ClassBlock and self.cur.in_conditional_block)
             or self.cur.type == ModuleBlock)
                and not self.cur.has_conditional_annotations):
            self.cur.has_conditional_annotations = 1
            self.add_def("__conditional_annotations__", USE, LOCATION(annotation))
        parent_ste = self.cur
        if parent_ste.annotation_block is None:
            current_type = parent_ste.type
            self.enter_block("__annotate__", AnnotationBlock, key, LOCATION(annotation))
            parent_ste.annotation_block = self.cur
            future_annotations = self.future.ff_features & CO_FUTURE_ANNOTATIONS
            if current_type == ClassBlock and not future_annotations:
                self.cur.can_see_class_scope = 1
                parent_ste.needs_classdict = 1
                self.add_def("__classdict__", USE, LOCATION(annotation))
        else:
            self.enter_existing_block(parent_ste.annotation_block, False)
        if is_unevaluated:
            self.cur.in_unevaluated_annotation = 1
        try:
            self.visit_expr(annotation)
        finally:
            if is_unevaluated:
                self.cur.in_unevaluated_annotation = 0
        self.exit_block()

    def visit_argannotations(self, args):
        for arg in args:
            if arg.annotation is not None:
                self.cur.annotations_used = 1
                self.visit_expr(arg.annotation)

    def visit_annotations(self, o, a, returns, function_ste):
        is_in_class = self.cur.can_see_class_scope
        current_type = self.cur.type
        self.enter_block("__annotate__", AnnotationBlock, key_of(a), LOCATION(o))
        if is_in_class or current_type == ClassBlock:
            self.cur.can_see_class_scope = 1
            self.add_def("__classdict__", USE, LOCATION(o))
        if a.posonlyargs:
            self.visit_argannotations(a.posonlyargs)
        if a.args:
            self.visit_argannotations(a.args)
        if a.vararg is not None and a.vararg.annotation is not None:
            self.cur.annotations_used = 1
            self.visit_expr(a.vararg.annotation)
        if a.kwarg is not None and a.kwarg.annotation is not None:
            self.cur.annotations_used = 1
            self.visit_expr(a.kwarg.annotation)
        if a.kwonlyargs:
            self.visit_argannotations(a.kwonlyargs)
        if returns is not None:
            self.cur.annotations_used = 1
            self.visit_expr(returns)
        self.exit_block()

    def visit_arguments(self, a):
        if a.posonlyargs:
            self.visit_params(a.posonlyargs)
        if a.args:
            self.visit_params(a.args)
        if a.kwonlyargs:
            self.visit_params(a.kwonlyargs)
        if a.vararg is not None:
            self.add_def(a.vararg.arg, DEF_PARAM, LOCATION(a.vararg))
            self.cur.varargs = 1
        if a.kwarg is not None:
            self.add_def(a.kwarg.arg, DEF_PARAM, LOCATION(a.kwarg))
            self.cur.varkeywords = 1

    def visit_excepthandler(self, eh):
        if eh.type is not None:
            self.visit_expr(eh.type)
        if eh.name is not None:
            self.add_def(eh.name, DEF_LOCAL, LOCATION(eh))
        self.visit_stmts(eh.body)

    def visit_withitem(self, item):
        self.visit_expr(item.context_expr)
        if item.optional_vars is not None:
            self.visit_expr(item.optional_vars)

    def visit_match_case(self, m):
        self.visit_pattern(m.pattern)
        if m.guard is not None:
            self.visit_expr(m.guard)
        self.visit_stmts(m.body)

    def visit_alias(self, a):
        name = a.name if a.asname is None else a.asname
        dot = name.find('.')
        if dot != -1:
            store_name = name[:dot]
        else:
            store_name = name
        if name != "*":
            self.add_def(store_name, DEF_IMPORT, LOCATION(a))
        else:
            if self.cur.type != ModuleBlock:
                self._error(IMPORT_STAR_WARNING, LOCATION(a))

    def visit_comprehension(self, lc):
        self.cur.comp_iter_target = 1
        self.visit_expr(lc.target)
        self.cur.comp_iter_target = 0
        self.cur.comp_iter_expr += 1
        self.visit_expr(lc.iter)
        self.cur.comp_iter_expr -= 1
        self.visit_exprs(lc.ifs)
        if lc.is_async:
            self.cur.coroutine = 1

    def visit_keyword(self, k):
        self.visit_expr(k.value)

    def handle_comprehension(self, e, scope_name, generators, elt, value):
        is_generator = isinstance(e, ast.GeneratorExp)
        outermost = generators[0]
        self.cur.comp_iter_expr += 1
        self.visit_expr(outermost.iter)
        self.cur.comp_iter_expr -= 1
        self.enter_block(scope_name, FunctionBlock, key_of(e), LOCATION(e))
        k = type(e)
        if k is ast.ListComp:
            self.cur.comprehension = ListComprehension
        elif k is ast.SetComp:
            self.cur.comprehension = SetComprehension
        elif k is ast.DictComp:
            self.cur.comprehension = DictComprehension
        else:
            self.cur.comprehension = GeneratorExpression
        if outermost.is_async:
            self.cur.coroutine = 1
        self.implicit_arg(0)
        self.cur.comp_iter_target = 1
        self.visit_expr(outermost.target)
        self.cur.comp_iter_target = 0
        self.visit_exprs(outermost.ifs)
        for comp in generators[1:]:
            self.visit_comprehension(comp)
        if value is not None:
            self.visit_expr(value)
        self.visit_expr(elt)
        self.cur.generator = is_generator
        is_async = self.cur.coroutine and not is_generator
        self.exit_block()
        if (is_async and not self.is_async_def()
                and self.cur.comprehension == NoComprehension
                and not self.allows_top_level_await()):
            self._error("asynchronous comprehension outside of an asynchronous function",
                        LOCATION(e))
        if is_async:
            self.cur.coroutine = 1

    def raise_if_annotation_block(self, name, e):
        t = self.cur.type
        if t == AnnotationBlock:
            msg = ANNOTATION_NOT_ALLOWED % name
        elif t == TypeVariableBlock:
            msg = EXPR_NOT_ALLOWED_IN_TYPE_VARIABLE % (name, self.cur.scope_info)
        elif t == TypeAliasBlock:
            msg = EXPR_NOT_ALLOWED_IN_TYPE_ALIAS % name
        elif t == TypeParametersBlock:
            msg = EXPR_NOT_ALLOWED_IN_TYPE_PARAMETERS % name
        else:
            return
        self._error(msg, LOCATION(e))

    def raise_if_comprehension_block(self, e):
        t = self.cur.comprehension
        msg = ("'yield' inside list comprehension" if t == ListComprehension else
               "'yield' inside set comprehension" if t == SetComprehension else
               "'yield' inside dict comprehension" if t == DictComprehension else
               "'yield' inside generator expression")
        self._error(msg, LOCATION(e))

    def raise_if_not_coroutine(self, msg, loc):
        if not self.cur.coroutine:
            self._error(msg, loc)


def build(mod, filename, future):
    """_PySymtable_Build."""
    st = SymTable(filename, future)
    loc0 = SourceLocation(0, 0, 0, 0)
    st.enter_block("top", ModuleBlock, key_of(mod), loc0)
    st.top = st.cur
    if isinstance(mod, ast.Module):
        seq = mod.body
        if get_docstring(seq) is not None:
            st.cur.has_docstring = 1
        for s in seq:
            st.visit_stmt(s)
    elif isinstance(mod, ast.Expression):
        st.visit_expr(mod.body)
    elif isinstance(mod, ast.Interactive):
        for s in mod.body:
            st.visit_stmt(s)
    else:
        raise RuntimeError("this compiler does not handle FunctionTypes")
    st.exit_block()
    st.analyze()
    return st
