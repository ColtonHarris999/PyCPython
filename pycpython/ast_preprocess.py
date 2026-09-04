"""Port of Python/ast_preprocess.c: AST pre-processing before compilation.

In 3.14 most constant folding lives in the CFG optimizer; this pass only
handles ``__debug__`` substitution, docstring removal under -OO, the
printf-style format optimisation for ``"..." % (...)``, docstring
JoinedStr wrapping and the PEP 765 control-flow-in-finally warnings.
"""

from . import pyast as ast
from .errors import emit_syntax_warning
from .future import get_docstring, CO_FUTURE_ANNOTATIONS

MAXDIGITS = 3

F_LJUST = 1 << 0
F_SIGN = 1 << 1
F_BLANK = 1 << 2
F_ALT = 1 << 3
F_ZERO = 1 << 4


class _State:
    def __init__(self, filename, optimize, ff_features, syntax_check_only, enable_warnings):
        self.filename = filename
        self.optimize = optimize
        self.ff_features = ff_features
        self.syntax_check_only = syntax_check_only
        self.enable_warnings = enable_warnings
        self.cf_finally = []

    # PEP 765 context tracking
    def push(self, finally_, funcdef, loop):
        self.cf_finally.append((finally_, funcdef, loop))

    def pop(self):
        self.cf_finally.pop()

    def top(self):
        return self.cf_finally[-1]


def _control_flow_in_finally_warning(kw, n, state):
    emit_syntax_warning("'%s' in a 'finally' block" % kw, state.filename, n.lineno,
                        n.col_offset + 1, n.end_lineno, n.end_col_offset + 1)


def _before_return(state, node):
    if state.enable_warnings and state.cf_finally:
        in_finally, in_funcdef, in_loop = state.top()
        if in_finally and not in_funcdef:
            _control_flow_in_finally_warning("return", node, state)


def _before_loop_exit(state, node, kw):
    if state.enable_warnings and state.cf_finally:
        in_finally, in_funcdef, in_loop = state.top()
        if in_finally and not in_loop:
            _control_flow_in_finally_warning(kw, node, state)


def _make_const(node, val):
    """Turn *node* into a Constant holding *val* in place."""
    node.__class__ = ast.Constant
    node.value = val
    node.kind = None


def _has_starred(elts):
    return any(isinstance(e, ast.Starred) for e in elts)


def _parse_literal(fmt, pos):
    size = len(fmt)
    start = pos
    has_percents = False
    while pos < size:
        if fmt[pos] != '%':
            pos += 1
        elif pos + 1 < size and fmt[pos + 1] == '%':
            has_percents = True
            pos += 2
        else:
            break
    if pos == start:
        return None, pos
    s = fmt[start:pos]
    if has_percents:
        s = s.replace('%%', '%')
    return ast.Constant(s, None, -1, -1, -1, -1), pos


def _simple_format_arg_parse(fmt, pos):
    """Returns (ok, pos, spec, flags, width, prec)."""
    n = len(fmt)
    flags = 0
    width = -1
    prec = -1

    def nextc(pos):
        if pos >= n:
            return None, pos
        return fmt[pos], pos + 1

    while True:
        ch, pos = nextc(pos)
        if ch is None:
            return False, pos, None, flags, width, prec
        if ch == '-':
            flags |= F_LJUST
            continue
        if ch == '+':
            flags |= F_SIGN
            continue
        if ch == ' ':
            flags |= F_BLANK
            continue
        if ch == '#':
            flags |= F_ALT
            continue
        if ch == '0':
            flags |= F_ZERO
            continue
        break
    if '0' <= ch <= '9':
        width = 0
        digits = 0
        while '0' <= ch <= '9':
            width = width * 10 + (ord(ch) - 48)
            ch, pos = nextc(pos)
            if ch is None:
                return False, pos, None, flags, width, prec
            digits += 1
            if digits >= MAXDIGITS:
                return False, pos, None, flags, width, prec
    if ch == '.':
        ch, pos = nextc(pos)
        if ch is None:
            return False, pos, None, flags, width, prec
        prec = 0
        if '0' <= ch <= '9':
            digits = 0
            while '0' <= ch <= '9':
                prec = prec * 10 + (ord(ch) - 48)
                ch, pos = nextc(pos)
                if ch is None:
                    return False, pos, None, flags, width, prec
                digits += 1
                if digits >= MAXDIGITS:
                    return False, pos, None, flags, width, prec
    return True, pos, ch, flags, width, prec


def _parse_format(fmt, pos, arg):
    ok, pos, spec, flags, width, prec = _simple_format_arg_parse(fmt, pos)
    if not ok:
        return None, pos
    if spec in ('s', 'r', 'a'):
        buf = ''
        if not (flags & F_LJUST) and width > 0:
            buf += '>'
        if width >= 0:
            buf += str(width)
        if prec >= 0:
            buf += '.%d' % prec
        format_spec = None
        if buf:
            format_spec = ast.Constant(buf, None, -1, -1, -1, -1)
        return ast.FormattedValue(arg, ord(spec), format_spec, arg.lineno, arg.col_offset,
                                  arg.end_lineno, arg.end_col_offset), pos
    return None, pos


def _optimize_format(node, fmt, elts):
    pos = 0
    cnt = 0
    seq = []
    while True:
        lit, pos = _parse_literal(fmt, pos)
        if lit is not None:
            seq.append(lit)
        if pos >= len(fmt):
            break
        if cnt >= len(elts):
            return
        assert fmt[pos] == '%'
        pos += 1
        expr, pos = _parse_format(fmt, pos, elts[cnt])
        cnt += 1
        if expr is None:
            return
        seq.append(expr)
    if cnt < len(elts):
        return
    res = ast.JoinedStr(seq, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
    _copy_node(node, res)


def _copy_node(to, frm):
    to.__class__ = type(frm)
    for f in frm._fields:
        setattr(to, f, getattr(frm, f))
    for a in frm._attributes:
        setattr(to, a, getattr(frm, a))


def _fold_binop(node, state):
    if state.syntax_check_only:
        return
    lhs = node.left
    rhs = node.right
    if not isinstance(lhs, ast.Constant):
        return
    lv = lhs.value
    if (node.op is ast.Mod_singleton and isinstance(rhs, ast.Tuple)
            and isinstance(lv, str) and not _has_starred(rhs.elts)):
        _optimize_format(node, lv, rhs.elts)


def _remove_docstring(stmts, idx):
    if len(stmts) == 1:
        docstring = stmts[0]
        stmts[0] = ast.Pass(docstring.lineno, docstring.col_offset,
                            docstring.lineno, docstring.col_offset + 4)
        return
    del stmts[idx]


def _fold_body(stmts, state):
    docstring = get_docstring(stmts) is not None
    if docstring and state.optimize >= 2:
        _remove_docstring(stmts, 0)
        docstring = False
    for s in stmts:
        _fold_stmt(s, state)
    if not docstring and get_docstring(stmts) is not None:
        st = stmts[0]
        expr = ast.JoinedStr([st.value], st.lineno, st.col_offset, st.end_lineno,
                             st.end_col_offset)
        st.value = expr


def _fold_mod(node, state):
    if isinstance(node, ast.Module):
        _fold_body(node.body, state)
    elif isinstance(node, ast.Interactive):
        for s in node.body:
            _fold_stmt(s, state)
    elif isinstance(node, ast.Expression):
        _fold_expr(node.body, state)


def _fold_exprs(seq, state):
    for e in seq:
        if e is not None:
            _fold_expr(e, state)


def _fold_expr(node, state):
    k = type(node)
    if k is ast.BoolOp:
        _fold_exprs(node.values, state)
    elif k is ast.BinOp:
        _fold_expr(node.left, state)
        _fold_expr(node.right, state)
        _fold_binop(node, state)
    elif k is ast.UnaryOp:
        _fold_expr(node.operand, state)
    elif k is ast.Lambda:
        _fold_arguments(node.args, state)
        _fold_expr(node.body, state)
    elif k is ast.IfExp:
        _fold_expr(node.test, state)
        _fold_expr(node.body, state)
        _fold_expr(node.orelse, state)
    elif k is ast.Dict:
        _fold_exprs(node.keys, state)
        _fold_exprs(node.values, state)
    elif k is ast.Set:
        _fold_exprs(node.elts, state)
    elif k is ast.ListComp or k is ast.SetComp or k is ast.GeneratorExp:
        _fold_expr(node.elt, state)
        for c in node.generators:
            _fold_comprehension(c, state)
    elif k is ast.DictComp:
        _fold_expr(node.key, state)
        _fold_expr(node.value, state)
        for c in node.generators:
            _fold_comprehension(c, state)
    elif k is ast.Await:
        _fold_expr(node.value, state)
    elif k is ast.Yield:
        if node.value is not None:
            _fold_expr(node.value, state)
    elif k is ast.YieldFrom:
        _fold_expr(node.value, state)
    elif k is ast.Compare:
        _fold_expr(node.left, state)
        _fold_exprs(node.comparators, state)
    elif k is ast.Call:
        _fold_expr(node.func, state)
        _fold_exprs(node.args, state)
        for kw in node.keywords:
            if kw is not None:
                _fold_expr(kw.value, state)
    elif k is ast.FormattedValue or k is ast.Interpolation:
        _fold_expr(node.value, state)
        if node.format_spec is not None:
            _fold_expr(node.format_spec, state)
    elif k is ast.JoinedStr or k is ast.TemplateStr:
        _fold_exprs(node.values, state)
    elif k is ast.Attribute:
        _fold_expr(node.value, state)
    elif k is ast.Subscript:
        _fold_expr(node.value, state)
        _fold_expr(node.slice, state)
    elif k is ast.Starred:
        _fold_expr(node.value, state)
    elif k is ast.Slice:
        if node.lower is not None:
            _fold_expr(node.lower, state)
        if node.upper is not None:
            _fold_expr(node.upper, state)
        if node.step is not None:
            _fold_expr(node.step, state)
    elif k is ast.List or k is ast.Tuple:
        _fold_exprs(node.elts, state)
    elif k is ast.Name:
        if state.syntax_check_only:
            return
        if node.ctx is ast.Load_singleton and node.id == "__debug__":
            _make_const(node, not state.optimize)
    elif k is ast.NamedExpr:
        _fold_expr(node.value, state)
    elif k is ast.Constant:
        pass


def _fold_comprehension(node, state):
    _fold_expr(node.target, state)
    _fold_expr(node.iter, state)
    _fold_exprs(node.ifs, state)


def _fold_arguments(node, state):
    for a in node.posonlyargs:
        _fold_arg(a, state)
    for a in node.args:
        _fold_arg(a, state)
    if node.vararg is not None:
        _fold_arg(node.vararg, state)
    for a in node.kwonlyargs:
        _fold_arg(a, state)
    _fold_exprs(node.kw_defaults, state)
    if node.kwarg is not None:
        _fold_arg(node.kwarg, state)
    _fold_exprs(node.defaults, state)


def _fold_arg(node, state):
    if not (state.ff_features & CO_FUTURE_ANNOTATIONS):
        if node.annotation is not None:
            _fold_expr(node.annotation, state)


def _fold_stmts(seq, state):
    for s in seq:
        _fold_stmt(s, state)


def _fold_stmt(node, state):
    k = type(node)
    if k is ast.FunctionDef or k is ast.AsyncFunctionDef:
        for tp in node.type_params:
            _fold_type_param(tp, state)
        _fold_arguments(node.args, state)
        state.push(False, True, False)
        _fold_body(node.body, state)
        state.pop()
        _fold_exprs(node.decorator_list, state)
        if not (state.ff_features & CO_FUTURE_ANNOTATIONS):
            if node.returns is not None:
                _fold_expr(node.returns, state)
    elif k is ast.ClassDef:
        for tp in node.type_params:
            _fold_type_param(tp, state)
        _fold_exprs(node.bases, state)
        for kw in node.keywords:
            _fold_expr(kw.value, state)
        _fold_body(node.body, state)
        _fold_exprs(node.decorator_list, state)
    elif k is ast.Return:
        _before_return(state, node)
        if node.value is not None:
            _fold_expr(node.value, state)
    elif k is ast.Delete:
        _fold_exprs(node.targets, state)
    elif k is ast.Assign:
        _fold_exprs(node.targets, state)
        _fold_expr(node.value, state)
    elif k is ast.AugAssign:
        _fold_expr(node.target, state)
        _fold_expr(node.value, state)
    elif k is ast.AnnAssign:
        _fold_expr(node.target, state)
        if not (state.ff_features & CO_FUTURE_ANNOTATIONS):
            _fold_expr(node.annotation, state)
        if node.value is not None:
            _fold_expr(node.value, state)
    elif k is ast.TypeAlias:
        _fold_expr(node.name, state)
        for tp in node.type_params:
            _fold_type_param(tp, state)
        _fold_expr(node.value, state)
    elif k is ast.For or k is ast.AsyncFor:
        _fold_expr(node.target, state)
        _fold_expr(node.iter, state)
        state.push(False, False, True)
        _fold_stmts(node.body, state)
        state.pop()
        _fold_stmts(node.orelse, state)
    elif k is ast.While:
        _fold_expr(node.test, state)
        state.push(False, False, True)
        _fold_stmts(node.body, state)
        state.pop()
        _fold_stmts(node.orelse, state)
    elif k is ast.If:
        _fold_expr(node.test, state)
        _fold_stmts(node.body, state)
        _fold_stmts(node.orelse, state)
    elif k is ast.With or k is ast.AsyncWith:
        for item in node.items:
            _fold_expr(item.context_expr, state)
            if item.optional_vars is not None:
                _fold_expr(item.optional_vars, state)
        _fold_stmts(node.body, state)
    elif k is ast.Raise:
        if node.exc is not None:
            _fold_expr(node.exc, state)
        if node.cause is not None:
            _fold_expr(node.cause, state)
    elif k is ast.Try or k is ast.TryStar:
        _fold_stmts(node.body, state)
        for h in node.handlers:
            if h.type is not None:
                _fold_expr(h.type, state)
            _fold_stmts(h.body, state)
        _fold_stmts(node.orelse, state)
        state.push(True, False, False)
        _fold_stmts(node.finalbody, state)
        state.pop()
    elif k is ast.Assert:
        _fold_expr(node.test, state)
        if node.msg is not None:
            _fold_expr(node.msg, state)
    elif k is ast.Expr:
        _fold_expr(node.value, state)
    elif k is ast.Match:
        _fold_expr(node.subject, state)
        for mc in node.cases:
            _fold_pattern(mc.pattern, state)
            if mc.guard is not None:
                _fold_expr(mc.guard, state)
            _fold_stmts(mc.body, state)
    elif k is ast.Break:
        _before_loop_exit(state, node, "break")
    elif k is ast.Continue:
        _before_loop_exit(state, node, "continue")
    elif k in (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.Pass):
        pass


def _fold_const_match_patterns(node, state):
    if state.syntax_check_only:
        return
    if isinstance(node, ast.UnaryOp):
        if node.op is ast.USub_singleton and isinstance(node.operand, ast.Constant):
            operand = node.operand.value
            try:
                folded = -operand
            except Exception:
                return
            _make_const(node, folded)
    elif isinstance(node, ast.BinOp):
        op = node.op
        if ((op is ast.Add_singleton or op is ast.Sub_singleton)
                and isinstance(node.right, ast.Constant)):
            _fold_const_match_patterns(node.left, state)
            if isinstance(node.left, ast.Constant):
                left = node.left.value
                right = node.right.value
                try:
                    folded = left + right if op is ast.Add_singleton else left - right
                except Exception:
                    return
                _make_const(node, folded)


def _fold_pattern(node, state):
    k = type(node)
    if k is ast.MatchValue:
        _fold_const_match_patterns(node.value, state)
    elif k is ast.MatchSingleton:
        pass
    elif k is ast.MatchSequence:
        for p in node.patterns:
            _fold_pattern(p, state)
    elif k is ast.MatchMapping:
        for key in node.keys:
            _fold_const_match_patterns(key, state)
        for p in node.patterns:
            _fold_pattern(p, state)
    elif k is ast.MatchClass:
        _fold_expr(node.cls, state)
        for p in node.patterns:
            _fold_pattern(p, state)
        for p in node.kwd_patterns:
            _fold_pattern(p, state)
    elif k is ast.MatchStar:
        pass
    elif k is ast.MatchAs:
        if node.pattern is not None:
            _fold_pattern(node.pattern, state)
    elif k is ast.MatchOr:
        for p in node.patterns:
            _fold_pattern(p, state)


def _fold_type_param(node, state):
    if isinstance(node, ast.TypeVar):
        if node.bound is not None:
            _fold_expr(node.bound, state)
        if node.default_value is not None:
            _fold_expr(node.default_value, state)
    elif isinstance(node, (ast.ParamSpec, ast.TypeVarTuple)):
        if node.default_value is not None:
            _fold_expr(node.default_value, state)


def preprocess(mod, filename, optimize, ff_features, syntax_check_only, enable_warnings):
    """_PyAST_Preprocess."""
    state = _State(filename, optimize, ff_features, syntax_check_only, enable_warnings)
    _fold_mod(mod, state)
    return True
