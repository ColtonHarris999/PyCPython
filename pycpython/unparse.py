"""Port of Python/ast_unparse.c: the limited expression unparser used to
stringify annotations under ``from __future__ import annotations``."""

from . import pyast as ast

PR_TUPLE = 0
PR_TEST = 1
PR_OR = 2
PR_AND = 3
PR_NOT = 4
PR_CMP = 5
PR_EXPR = 6
PR_BOR = PR_EXPR
PR_BXOR = 7
PR_BAND = 8
PR_SHIFT = 9
PR_ARITH = 10
PR_TERM = 11
PR_FACTOR = 12
PR_POWER = 13
PR_AWAIT = 14
PR_ATOM = 15

_BINOPS = {
    ast.Add: (" + ", PR_ARITH, False),
    ast.Sub: (" - ", PR_ARITH, False),
    ast.Mult: (" * ", PR_TERM, False),
    ast.MatMult: (" @ ", PR_TERM, False),
    ast.Div: (" / ", PR_TERM, False),
    ast.Mod: (" % ", PR_TERM, False),
    ast.LShift: (" << ", PR_SHIFT, False),
    ast.RShift: (" >> ", PR_SHIFT, False),
    ast.BitOr: (" | ", PR_BOR, False),
    ast.BitXor: (" ^ ", PR_BXOR, False),
    ast.BitAnd: (" & ", PR_BAND, False),
    ast.FloorDiv: (" // ", PR_TERM, False),
    ast.Pow: (" ** ", PR_POWER, True),
}

_UNARYOPS = {
    ast.Invert: ("~", PR_FACTOR),
    ast.Not: ("not ", PR_NOT),
    ast.UAdd: ("+", PR_FACTOR),
    ast.USub: ("-", PR_FACTOR),
}

_CMPOPS = {
    ast.Eq: " == ", ast.NotEq: " != ", ast.Lt: " < ", ast.LtE: " <= ",
    ast.Gt: " > ", ast.GtE: " >= ", ast.Is: " is ", ast.IsNot: " is not ",
    ast.In: " in ", ast.NotIn: " not in ",
}


def _repr(obj):
    r = repr(obj)
    if (type(obj) is float and obj in (float('inf'), float('-inf'))) or type(obj) is complex:
        r = r.replace("inf", "1e309")
    return r


class _Writer:
    def __init__(self):
        self.parts = []

    def write(self, s):
        self.parts.append(s)

    def finish(self):
        return "".join(self.parts)


def _boolop(w, e, level):
    op_s = " and " if isinstance(e.op, ast.And) else " or "
    pr = PR_AND if isinstance(e.op, ast.And) else PR_OR
    if level > pr:
        w.write("(")
    for i, v in enumerate(e.values):
        if i > 0:
            w.write(op_s)
        _expr(w, v, pr + 1)
    if level > pr:
        w.write(")")


def _binop(w, e, level):
    op_s, pr, rassoc = _BINOPS[type(e.op)]
    if level > pr:
        w.write("(")
    _expr(w, e.left, pr + (1 if rassoc else 0))
    w.write(op_s)
    _expr(w, e.right, pr + (0 if rassoc else 1))
    if level > pr:
        w.write(")")


def _unaryop(w, e, level):
    op_s, pr = _UNARYOPS[type(e.op)]
    if level > pr:
        w.write("(")
    w.write(op_s)
    _expr(w, e.operand, pr)
    if level > pr:
        w.write(")")


def _arg(w, arg):
    w.write(arg.arg)
    if arg.annotation is not None:
        w.write(": ")
        _expr(w, arg.annotation, PR_TEST)


def _args(w, args):
    first = [True]

    def sep():
        if not first[0]:
            w.write(", ")
        first[0] = False

    posonlyarg_count = len(args.posonlyargs)
    arg_count = len(args.args)
    default_count = len(args.defaults)
    for i in range(posonlyarg_count + arg_count):
        sep()
        if i < posonlyarg_count:
            _arg(w, args.posonlyargs[i])
        else:
            _arg(w, args.args[i - posonlyarg_count])
        di = i - posonlyarg_count - arg_count + default_count
        if di >= 0:
            w.write("=")
            _expr(w, args.defaults[di], PR_TEST)
        if posonlyarg_count and i + 1 == posonlyarg_count:
            w.write(", /")
    if args.vararg is not None or len(args.kwonlyargs):
        sep()
        w.write("*")
        if args.vararg is not None:
            _arg(w, args.vararg)
    arg_count = len(args.kwonlyargs)
    default_count = len(args.kw_defaults)
    for i in range(arg_count):
        sep()
        _arg(w, args.kwonlyargs[i])
        di = i - arg_count + default_count
        if di >= 0:
            default_ = args.kw_defaults[di]
            if default_ is not None:
                w.write("=")
                _expr(w, default_, PR_TEST)
    if args.kwarg is not None:
        sep()
        w.write("**")
        _arg(w, args.kwarg)


def _lambda(w, e, level):
    if level > PR_TEST:
        w.write("(")
    n_positional = len(e.args.args) + len(e.args.posonlyargs)
    w.write("lambda " if n_positional else "lambda")
    _args(w, e.args)
    w.write(": ")
    _expr(w, e.body, PR_TEST)
    if level > PR_TEST:
        w.write(")")


def _ifexp(w, e, level):
    if level > PR_TEST:
        w.write("(")
    _expr(w, e.body, PR_TEST + 1)
    w.write(" if ")
    _expr(w, e.test, PR_TEST + 1)
    w.write(" else ")
    _expr(w, e.orelse, PR_TEST)
    if level > PR_TEST:
        w.write(")")


def _dict(w, e):
    w.write("{")
    for i, value in enumerate(e.values):
        if i > 0:
            w.write(", ")
        key = e.keys[i]
        if key is not None:
            _expr(w, key, PR_TEST)
            w.write(": ")
            _expr(w, value, PR_TEST)
        else:
            w.write("**")
            _expr(w, value, PR_EXPR)
    w.write("}")


def _seq(w, elts, open_, close):
    w.write(open_)
    for i, elt in enumerate(elts):
        if i > 0:
            w.write(", ")
        _expr(w, elt, PR_TEST)
    w.write(close)


def _tuple(w, e, level):
    n = len(e.elts)
    if n == 0:
        w.write("()")
        return
    if level > PR_TUPLE:
        w.write("(")
    for i, elt in enumerate(e.elts):
        if i > 0:
            w.write(", ")
        _expr(w, elt, PR_TEST)
    if n == 1:
        w.write(",")
    if level > PR_TUPLE:
        w.write(")")


def _comprehension(w, gen):
    w.write(" async for " if gen.is_async else " for ")
    _expr(w, gen.target, PR_TUPLE)
    w.write(" in ")
    _expr(w, gen.iter, PR_TEST + 1)
    for cond in gen.ifs:
        w.write(" if ")
        _expr(w, cond, PR_TEST + 1)


def _comprehensions(w, gens):
    for g in gens:
        _comprehension(w, g)


def _genexp(w, e):
    w.write("(")
    _expr(w, e.elt, PR_TEST)
    _comprehensions(w, e.generators)
    w.write(")")


def _compare(w, e, level):
    if level > PR_CMP:
        w.write("(")
    _expr(w, e.left, PR_CMP + 1)
    for i, comp in enumerate(e.comparators):
        w.write(_CMPOPS[type(e.ops[i])])
        _expr(w, comp, PR_CMP + 1)
    if level > PR_CMP:
        w.write(")")


def _keyword(w, kw):
    if kw.arg is None:
        w.write("**")
    else:
        w.write(kw.arg)
        w.write("=")
    _expr(w, kw.value, PR_TEST)


def _call(w, e):
    _expr(w, e.func, PR_ATOM)
    if len(e.args) == 1 and len(e.keywords) == 0:
        a = e.args[0]
        if isinstance(a, ast.GeneratorExp):
            _genexp(w, a)
            return
    w.write("(")
    first = True
    for a in e.args:
        if not first:
            w.write(", ")
        first = False
        _expr(w, a, PR_TEST)
    for kw in e.keywords:
        if not first:
            w.write(", ")
        first = False
        _keyword(w, kw)
    w.write(")")


def _escape_braces(s):
    return s.replace("{", "{{").replace("}", "}}")


def _fstring_element(w, e, is_format_spec):
    if isinstance(e, ast.Constant):
        w.write(_escape_braces(e.value))
    elif isinstance(e, ast.JoinedStr):
        _joinedstr(w, e, is_format_spec)
    elif isinstance(e, ast.TemplateStr):
        _templatestr(w, e)
    elif isinstance(e, ast.FormattedValue):
        _formattedvalue(w, e)
    elif isinstance(e, ast.Interpolation):
        _interpolation(w, e)
    else:
        raise SystemError("unknown expression kind inside f-string or t-string")


def _build_ftstring_body(values, is_format_spec):
    bw = _Writer()
    for v in values:
        _fstring_element(bw, v, is_format_spec)
    return bw.finish()


def _templatestr(w, e):
    body = _build_ftstring_body(e.values, False)
    w.write("t")
    w.write(_repr(body))


def _joinedstr(w, e, is_format_spec):
    body = _build_ftstring_body(e.values, is_format_spec)
    if not is_format_spec:
        w.write("f")
        w.write(_repr(body))
    else:
        w.write(body)


def _interpolation_str(w, s):
    outer_brace = "{"
    if s.startswith("{"):
        outer_brace = "{ "
    w.write(outer_brace)
    w.write(s)


def _interpolation_value(w, e):
    temp = expr_as_unicode(e, PR_TEST + 1)
    _interpolation_str(w, temp)


def _interpolation_conversion(w, conversion):
    if conversion is None or conversion < 0:
        return
    if conversion == ord('a'):
        w.write("!a")
    elif conversion == ord('r'):
        w.write("!r")
    elif conversion == ord('s'):
        w.write("!s")
    else:
        raise SystemError("unknown f-value conversion kind")


def _interpolation_format_spec(w, e):
    if e is not None:
        w.write(":")
        _fstring_element(w, e, True)


def _interpolation(w, e):
    _interpolation_str(w, e.str)
    _interpolation_conversion(w, e.conversion)
    _interpolation_format_spec(w, e.format_spec)
    w.write("}")


def _formattedvalue(w, e):
    _interpolation_value(w, e.value)
    _interpolation_conversion(w, e.conversion)
    _interpolation_format_spec(w, e.format_spec)
    w.write("}")


def _constant(w, constant):
    if type(constant) is tuple:
        w.write("(")
        for i, item in enumerate(constant):
            if i > 0:
                w.write(", ")
            _constant(w, item)
        if len(constant) == 1:
            w.write(",")
        w.write(")")
        return
    w.write(_repr(constant))


def _attribute(w, e):
    v = e.value
    _expr(w, v, PR_ATOM)
    if isinstance(v, ast.Constant) and type(v.value) is int:
        w.write(" .")
    else:
        w.write(".")
    w.write(e.attr)


def _slice(w, e):
    if e.lower is not None:
        _expr(w, e.lower, PR_TEST)
    w.write(":")
    if e.upper is not None:
        _expr(w, e.upper, PR_TEST)
    if e.step is not None:
        w.write(":")
        _expr(w, e.step, PR_TEST)


def _expr(w, e, level):
    if isinstance(e, ast.BoolOp):
        _boolop(w, e, level)
    elif isinstance(e, ast.BinOp):
        _binop(w, e, level)
    elif isinstance(e, ast.UnaryOp):
        _unaryop(w, e, level)
    elif isinstance(e, ast.Lambda):
        _lambda(w, e, level)
    elif isinstance(e, ast.IfExp):
        _ifexp(w, e, level)
    elif isinstance(e, ast.Dict):
        _dict(w, e)
    elif isinstance(e, ast.Set):
        _seq(w, e.elts, "{", "}")
    elif isinstance(e, ast.GeneratorExp):
        _genexp(w, e)
    elif isinstance(e, ast.ListComp):
        w.write("[")
        _expr(w, e.elt, PR_TEST)
        _comprehensions(w, e.generators)
        w.write("]")
    elif isinstance(e, ast.SetComp):
        w.write("{")
        _expr(w, e.elt, PR_TEST)
        _comprehensions(w, e.generators)
        w.write("}")
    elif isinstance(e, ast.DictComp):
        w.write("{")
        _expr(w, e.key, PR_TEST)
        w.write(": ")
        _expr(w, e.value, PR_TEST)
        _comprehensions(w, e.generators)
        w.write("}")
    elif isinstance(e, ast.Yield):
        if e.value is None:
            w.write("(yield)")
        else:
            w.write("(yield ")
            _expr(w, e.value, PR_TEST)
            w.write(")")
    elif isinstance(e, ast.YieldFrom):
        w.write("(yield from ")
        _expr(w, e.value, PR_TEST)
        w.write(")")
    elif isinstance(e, ast.Await):
        if level > PR_AWAIT:
            w.write("(")
        w.write("await ")
        _expr(w, e.value, PR_ATOM)
        if level > PR_AWAIT:
            w.write(")")
    elif isinstance(e, ast.Compare):
        _compare(w, e, level)
    elif isinstance(e, ast.Call):
        _call(w, e)
    elif isinstance(e, ast.Constant):
        if e.value is Ellipsis:
            w.write("...")
            return
        if e.kind is not None:
            w.write(e.kind)
        _constant(w, e.value)
    elif isinstance(e, ast.JoinedStr):
        _joinedstr(w, e, False)
    elif isinstance(e, ast.TemplateStr):
        _templatestr(w, e)
    elif isinstance(e, ast.FormattedValue):
        _formattedvalue(w, e)
    elif isinstance(e, ast.Interpolation):
        _interpolation(w, e)
    elif isinstance(e, ast.Attribute):
        _attribute(w, e)
    elif isinstance(e, ast.Subscript):
        _expr(w, e.value, PR_ATOM)
        w.write("[")
        _expr(w, e.slice, PR_TUPLE)
        w.write("]")
    elif isinstance(e, ast.Starred):
        w.write("*")
        _expr(w, e.value, PR_EXPR)
    elif isinstance(e, ast.Slice):
        _slice(w, e)
    elif isinstance(e, ast.Name):
        w.write(e.id)
    elif isinstance(e, ast.List):
        _seq(w, e.elts, "[", "]")
    elif isinstance(e, ast.Tuple):
        _tuple(w, e, level)
    elif isinstance(e, ast.NamedExpr):
        if level > PR_TUPLE:
            w.write("(")
        _expr(w, e.target, PR_ATOM)
        w.write(" := ")
        _expr(w, e.value, PR_ATOM)
        if level > PR_TUPLE:
            w.write(")")
    else:
        raise SystemError("unknown expression kind")


def expr_as_unicode(e, level=PR_TEST):
    w = _Writer()
    _expr(w, e, level)
    return w.finish()
