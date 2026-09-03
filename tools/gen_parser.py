"""Generate pycpython/parser/generated_parser.py from reference/python.gram.

This is a Python-emitting parser generator that reproduces the *semantics*
of CPython's C generator (Tools/peg_generator/pegen/c_generator.py):
memoization per token, left-recursion handling, the two-pass
``invalid_*`` rule scheme, ``EXTRA`` position tracking, cut/forced
tokens, etc.  The C actions in the grammar are translated into Python
expressions that call into ``pycpython.parser.pegen.Parser`` methods.

Tooling only: this file (and the vendored pegen package) is never imported
by the shipped library.

Usage: python tools/gen_parser.py reference/python.gram > pycpython/parser/generated_parser.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pegen.grammar import (Alt, Cut, Forced, Gather, Group, Lookahead,   # noqa: E402
                           NamedItem, NameLeaf, NegativeLookahead, Opt,
                           PositiveLookahead, Repeat0, Repeat1, Rhs, Rule,
                           StringLeaf, GrammarVisitor)
from pegen.parser_generator import ParserGenerator  # noqa: E402
from pegen.grammar_parser import GeneratedParser as GrammarParser  # noqa: E402
from pegen.tokenizer import Tokenizer  # noqa: E402
import tokenize  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
from pycpython import tokens as T  # noqa: E402

NON_EXACT_TOKENS = set()
EXACT_TOKENS = {}
ALL_TOKENS = {}
with open(os.path.join(HERE, '..', 'reference', 'Tokens')) as _f:
    _index = 0
    for _line in _f:
        _line = _line.strip()
        if not _line or _line.startswith('#'):
            continue
        _parts = _line.split()
        ALL_TOKENS[_index] = _parts[0]
        if len(_parts) > 1:
            EXACT_TOKENS[_parts[1].strip("'")] = _index
        else:
            NON_EXACT_TOKENS.add(_parts[0])
        _index += 1

BASE_NODETYPES = {"NAME", "NUMBER", "STRING", "SOFT_KEYWORD"}

SIMPLE_SUM_NAMES = {
    'Load', 'Store', 'Del', 'And', 'Or', 'Add', 'Sub', 'Mult', 'MatMult', 'Div',
    'Mod', 'Pow', 'LShift', 'RShift', 'BitOr', 'BitXor', 'BitAnd', 'FloorDiv',
    'Invert', 'Not', 'UAdd', 'USub', 'Eq', 'NotEq', 'Lt', 'LtE', 'Gt', 'GtE',
    'Is', 'IsNot', 'In', 'NotIn',
}

C_TYPE_NAMES = re.compile(r'^(?:\w+_ty|asdl_\w*seq|void|Token|AugOperator|'
                          r'CmpopExprPair|KeyValuePair|KeyPatternPair|NameDefaultPair|'
                          r'SlashWithDefault|StarEtc|KeywordOrStarred|'
                          r'ResultTokenWithMetadata|PyObject|int|expr_context_ty)$')

EXTRA_ARGS = "_start_lineno, _start_col_offset, _end_lineno, _end_col_offset"


# ----------------------------------------------------------------------
# C expression -> Python expression translator
# ----------------------------------------------------------------------

class CTok:
    __slots__ = ('kind', 'value')

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value

    def __repr__(self):
        return "CTok(%s,%r)" % (self.kind, self.value)


_C_TOKEN_RE = re.compile(r'''
    (?P<ws>\s+)
  | (?P<str>"(?:\\.|[^"\\])*")
  | (?P<num>\d+)
  | (?P<id>[A-Za-z_]\w*)
  | (?P<op>->|==|!=|&&|\|\||[-+*!?:,()\[\].<>])
''', re.X)


def ctokenize(s):
    out = []
    pos = 0
    while pos < len(s):
        m = _C_TOKEN_RE.match(s, pos)
        if not m:
            raise SyntaxError("cannot tokenize C action at %r" % s[pos:pos + 20])
        pos = m.end()
        kind = m.lastgroup
        if kind == 'ws':
            continue
        out.append(CTok(kind, m.group(kind)))
    out.append(CTok('eof', ''))
    return out


class ActionTranslator:
    """Translate one C action expression into a Python expression string."""

    def __init__(self, text):
        self.toks = ctokenize(text)
        self.i = 0

    def peek(self, k=0):
        return self.toks[self.i + k]

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, value):
        t = self.next()
        if t.value != value:
            raise SyntaxError("expected %r got %r" % (value, t.value))
        return t

    def translate(self):
        e = self.expr()
        if self.peek().kind != 'eof':
            raise SyntaxError("trailing junk in action: %r" % self.toks[self.i:])
        return e

    # expr := ternary
    def expr(self):
        cond = self.logical_or()
        if self.peek().value == '?':
            self.next()
            a = self.expr()
            self.expect(':')
            b = self.expr()
            return "(%s if %s else %s)" % (a, self.truthy(cond), b)
        return cond

    def truthy(self, cond):
        # C pointer truthiness for a bare variable / parenthesised variable
        inner = cond
        while inner.startswith('(') and inner.endswith(')'):
            inner = inner[1:-1]
        if re.match(r'^[A-Za-z_]\w*$', inner):
            return "%s is not None" % inner
        return cond

    def logical_or(self):
        a = self.logical_and()
        while self.peek().value == '||':
            self.next()
            b = self.logical_and()
            a = "(%s or %s)" % (a, b)
        return a

    def logical_and(self):
        a = self.equality()
        while self.peek().value == '&&':
            self.next()
            b = self.equality()
            a = "(%s and %s)" % (a, b)
        return a

    def equality(self):
        a = self.relational()
        while self.peek().value in ('==', '!='):
            op = self.next().value
            b = self.relational()
            m = re.match(r'^(\w+)_kind$', b)
            if m and a.endswith('.kind'):
                res = "isinstance(%s, ast.%s)" % (a[:-5], m.group(1))
                if op == '!=':
                    res = "(not %s)" % res
                a = res
            else:
                a = "(%s %s %s)" % (a, op, b)
        return a

    def relational(self):
        a = self.additive()
        while self.peek().value in ('<', '>'):
            op = self.next().value
            b = self.additive()
            a = "(%s %s %s)" % (a, op, b)
        return a

    def additive(self):
        a = self.unary()
        while self.peek().value in ('+', '-'):
            op = self.next().value
            b = self.unary()
            a = "(%s %s %s)" % (a, op, b)
        return a

    def unary(self):
        t = self.peek()
        if t.value == '!':
            self.next()
            return "(not %s)" % self.unary()
        if t.value == '-':
            self.next()
            return "(-%s)" % self.unary()
        if t.value == '(' and self.is_cast():
            # skip the cast entirely
            self.next()
            while self.peek().value != ')':
                self.next()
            self.next()
            return self.unary()
        return self.postfix()

    def is_cast(self):
        # '(' TYPE ['*'] ')'
        if self.peek(1).kind != 'id' or not C_TYPE_NAMES.match(self.peek(1).value):
            return False
        j = 2
        if self.peek(j).value == '*':
            j += 1
        return self.peek(j).value == ')'

    def postfix(self):
        e = self.primary()
        while True:
            t = self.peek()
            if t.value == '(':
                self.next()
                args = []
                if self.peek().value != ')':
                    while True:
                        if (self.peek().kind == 'id' and C_TYPE_NAMES.match(self.peek().value)
                                and self.peek(1).value in ('*', ',', ')')
                                and not (self.peek(1).value in (',', ')') and self.peek().value in ('p',))):
                            # a type argument (CHECK(type, ...))
                            typ = self.next().value
                            if self.peek().value == '*':
                                self.next()
                            args.append('__TYPE__' + typ)
                        else:
                            args.append(self.expr())
                        if self.peek().value == ',':
                            self.next()
                            continue
                        break
                self.expect(')')
                e = self.make_call(e, args)
            elif t.value == '->' or t.value == '.':
                self.next()
                field = self.next().value
                if field == 'v' and self.peek().value == '.':
                    # ->v.Kind.field
                    self.next()
                    self.next()  # Kind
                    self.expect('.')
                    field = self.next().value
                    e = "%s.%s" % (e, field)
                elif field == 'arena' and e == 'p':
                    e = '__ARENA__'
                elif e == 'p':
                    e = "self.%s" % field
                else:
                    e = "%s.%s" % (e, field)
            elif t.value == '[':
                self.next()
                idx = self.expr()
                self.expect(']')
                e = "%s[%s]" % (e, idx)
            else:
                break
        return e

    def primary(self):
        t = self.next()
        if t.kind == 'num':
            return t.value
        if t.kind == 'str':
            return self.c_string(t.value)
        if t.kind == 'id':
            return self.ident(t.value)
        if t.value == '(':
            e = self.expr()
            self.expect(')')
            return "(%s)" % e
        raise SyntaxError("unexpected token %r" % (t,))

    def c_string(self, s):
        body = s[1:-1]
        # C escapes -> Python literal escapes are compatible for the subset used
        return '"' + body + '"'

    def ident(self, name):
        if name == 'NULL':
            return 'None'
        if name == 'EXTRA':
            return '__EXTRA__'
        if name == 'Py_None':
            return 'None'
        if name == 'Py_True':
            return 'True'
        if name == 'Py_False':
            return 'False'
        if name == 'Py_Ellipsis':
            return 'Ellipsis'
        if name == 'PyExc_SyntaxError':
            return 'SyntaxError'
        if name in SIMPLE_SUM_NAMES:
            return 'ast.%s_singleton' % name
        if name in ('STAR_TARGETS', 'DEL_TARGETS', 'FOR_TARGETS'):
            return name
        if name == 'p':
            return 'p'
        return name

    def make_call(self, func, args):
        # drop arena / EXTRA expansion
        args = [a for a in args if a != '__ARENA__']
        expanded = []
        for a in args:
            if a == '__EXTRA__':
                expanded.append(EXTRA_ARGS)
            else:
                expanded.append(a)
        args = expanded
        if func.startswith('_PyAST_'):
            return "ast.%s(%s)" % (func[len('_PyAST_'):], ", ".join(args))
        if func.startswith('_PyPegen_'):
            if args and args[0] == 'p':
                args = args[1:]
            return "self.%s(%s)" % (func[len('_PyPegen_'):], ", ".join(args))
        if func in ('CHECK', 'CHECK_NULL_ALLOWED'):
            assert len(args) == 2
            return args[1]
        if func == 'CHECK_VERSION':
            assert len(args) == 4
            return "self.check_version(%s, %s, %s)" % (args[1], args[2], args[3])
        if func == 'NEW_TYPE_COMMENT':
            return "self.new_type_comment(%s)" % args[1]
        if func == 'RAISE_SYNTAX_ERROR':
            return "self.raise_syntax_error(%s)" % ", ".join(args)
        if func == 'RAISE_INDENTATION_ERROR':
            return "self.raise_indentation_error(%s)" % ", ".join(args)
        if func == 'RAISE_SYNTAX_ERROR_ON_NEXT_TOKEN':
            return "self.raise_syntax_error_on_next_token(%s)" % ", ".join(args)
        if func == 'RAISE_SYNTAX_ERROR_KNOWN_LOCATION':
            return "self.raise_syntax_error_known_location(%s)" % ", ".join(args)
        if func == 'RAISE_SYNTAX_ERROR_KNOWN_RANGE':
            return "self.raise_syntax_error_known_range(%s)" % ", ".join(args)
        if func == 'RAISE_SYNTAX_ERROR_STARTING_FROM':
            return "self.raise_syntax_error_starting_from(%s)" % ", ".join(args)
        if func == 'RAISE_SYNTAX_ERROR_INVALID_TARGET':
            return "self.raise_syntax_error_invalid_target(%s)" % ", ".join(args)
        if func == 'RAISE_ERROR_KNOWN_LOCATION':
            assert args[0] == 'p'
            return "self.raise_error_known_location(%s)" % ", ".join(args[1:])
        if func == 'PyPegen_first_item':
            return "%s[0]" % args[0]
        if func == 'PyPegen_last_item':
            return "%s[-1]" % args[0]
        if func == 'asdl_seq_LEN':
            return "len(%s)" % args[0]
        if func == 'asdl_seq_GET':
            return "%s[%s]" % (args[0], args[1])
        if func == 'PyBytes_AS_STRING':
            return "%s.decode('utf-8', 'surrogateescape')" % args[0]
        if func == 'PyErr_Occurred':
            return "False"
        raise SyntaxError("unknown C function in action: %s" % func)


def translate_action(action):
    action = re.sub(r'\s+', ' ', action.strip())
    return ActionTranslator(action).translate()


# ----------------------------------------------------------------------
# Call maker (mirrors CCallMakerVisitor)
# ----------------------------------------------------------------------

class FunctionCall:
    def __init__(self, function, arguments=(), assigned_variable=None,
                 nodetype=None, force_true=False, comment=None, is_lookahead=False):
        self.function = function
        self.arguments = list(arguments)
        self.assigned_variable = assigned_variable
        self.nodetype = nodetype
        self.force_true = force_true
        self.comment = comment
        self.is_lookahead = is_lookahead

    def call_text(self):
        return "%s(%s)" % (self.function, ", ".join(map(str, self.arguments)))

    def condition(self):
        """Python expression usable as an `and` operand."""
        call = self.call_text()
        if self.is_lookahead:
            return call
        if self.nodetype == 'CUT':
            return "(%s := True)" % self.assigned_variable
        if self.force_true:
            return "((%s := %s) is not None or True)" % (self.assigned_variable, call)
        if self.assigned_variable:
            return "(%s := %s) is not None" % (self.assigned_variable, call)
        return "%s is not None" % call


class PyCallMakerVisitor(GrammarVisitor):
    def __init__(self, gen):
        self.gen = gen
        self.cache = {}

    def keyword_helper(self, keyword):
        return FunctionCall(function="self.expect_token",
                            arguments=[self.gen.keywords[keyword]],
                            assigned_variable="_keyword", nodetype='KEYWORD',
                            comment="token='%s'" % keyword)

    def soft_keyword_helper(self, value):
        return FunctionCall(function="self.expect_soft_keyword",
                            arguments=[value],
                            assigned_variable="_keyword", nodetype='SOFT_KEYWORD',
                            comment="soft_keyword=%s" % value)

    def visit_NameLeaf(self, node):
        name = node.value
        if name in NON_EXACT_TOKENS:
            if name in BASE_NODETYPES:
                return FunctionCall(function="self.%s_token" % name.lower(),
                                    assigned_variable="%s_var" % name.lower(),
                                    nodetype=name, comment=name)
            return FunctionCall(function="self.expect_token",
                                arguments=["T.%s" % name],
                                assigned_variable="%s_var" % name.lower(),
                                nodetype='GENERIC_TOKEN', comment="token='%s'" % name)
        return FunctionCall(function="self._r_%s" % name,
                            assigned_variable="%s_var" % name,
                            comment=str(node))

    def visit_StringLeaf(self, node):
        import ast as _ast
        val = _ast.literal_eval(node.value)
        if re.match(r"[a-zA-Z_]\w*\Z", val):
            if node.value.endswith("'"):
                return self.keyword_helper(val)
            return self.soft_keyword_helper(node.value)
        assert val in EXACT_TOKENS, "%s is not a known literal" % node.value
        return FunctionCall(function="self.expect_token",
                            arguments=["T.%s" % ALL_TOKENS[EXACT_TOKENS[val]]],
                            assigned_variable="_literal", nodetype='GENERIC_TOKEN',
                            comment="token='%s'" % val)

    def visit_NamedItem(self, node):
        call = self.generate_call(node.item)
        if node.name:
            call.assigned_variable = node.name
        return call

    def lookahead_call_helper(self, node, positive):
        call = self.generate_call(node.node)
        return FunctionCall(function="self.lookahead",
                            arguments=["True" if positive else "False", call.function,
                                       *call.arguments],
                            is_lookahead=True)

    def visit_PositiveLookahead(self, node):
        return self.lookahead_call_helper(node, True)

    def visit_NegativeLookahead(self, node):
        return self.lookahead_call_helper(node, False)

    def visit_Forced(self, node):
        import ast as _ast
        if isinstance(node.node, StringLeaf):
            val = _ast.literal_eval(node.node.value)
            assert val in EXACT_TOKENS
            return FunctionCall(function="self.expect_forced_token",
                                arguments=["T.%s" % ALL_TOKENS[EXACT_TOKENS[val]],
                                           repr(val)],
                                assigned_variable="_literal", nodetype='GENERIC_TOKEN')
        if isinstance(node.node, Group):
            call = self.visit(node.node.rhs)
            call.assigned_variable = None
            return FunctionCall(function="self.expect_forced_result",
                                arguments=[call.call_text(), repr(str(node.node.rhs))],
                                assigned_variable="_literal")
        raise NotImplementedError

    def visit_Opt(self, node):
        call = self.generate_call(node.node)
        return FunctionCall(function=call.function, arguments=call.arguments,
                            assigned_variable="_opt_var", force_true=True,
                            comment=str(node))

    def _artificial(self, node, prefix, rule_generation_func):
        key = "%s_%s" % (prefix, node)
        if key in self.cache:
            name = self.cache[key]
        else:
            name = rule_generation_func()
            self.cache[key] = name
        return FunctionCall(function="self._r_%s" % name,
                            assigned_variable="%s_var" % name, comment=str(node))

    def visit_Rhs(self, node):
        if node.can_be_inlined:
            return self.generate_call(node.alts[0].items[0])
        return self._artificial(node, "rhs", lambda: self.gen.artificial_rule_from_rhs(node))

    def visit_Repeat0(self, node):
        return self._artificial(node, "repeat0",
                                lambda: self.gen.artificial_rule_from_repeat(node.node, False))

    def visit_Repeat1(self, node):
        return self._artificial(node, "repeat1",
                                lambda: self.gen.artificial_rule_from_repeat(node.node, True))

    def visit_Gather(self, node):
        return self._artificial(node, "gather",
                                lambda: self.gen.artificial_rule_from_gather(node))

    def visit_Group(self, node):
        return self.generate_call(node.rhs)

    def visit_Cut(self, node):
        return FunctionCall(function="True", assigned_variable="_cut_var", nodetype='CUT')

    def generate_call(self, node):
        return super().visit(node)


# ----------------------------------------------------------------------
# The generator
# ----------------------------------------------------------------------

class PyParserGenerator(ParserGenerator, GrammarVisitor):
    def __init__(self, grammar, file):
        tokens = set(ALL_TOKENS.values())
        super().__init__(grammar, tokens, file)
        self.callmakervisitor = PyCallMakerVisitor(self)
        self.without_invalid_depth = 0

    def generate(self, filename):
        self.collect_rules()
        self.print('"""Generated by tools/gen_parser.py from %s.  DO NOT EDIT."""'
                   % os.path.basename(filename))
        self.print("# flake8: noqa")
        self.print("from .. import tokens as T")
        self.print("from .. import pyast as ast")
        self.print("from .pegen import (Parser, _NO_MEMO, STAR_TARGETS, DEL_TARGETS, FOR_TARGETS)")
        self.print()
        self.print()
        # keywords
        self.print("KEYWORDS = {")
        with self.indent():
            for kw, typ in self.keywords.items():
                self.print("%r: %d," % (kw, typ))
        self.print("}")
        self.print("SOFT_KEYWORDS = %r" % (sorted(self.soft_keywords),))
        self.print()
        for i, (rulename, rule) in enumerate(self.all_rules.items(), 1000):
            comment = "  # Left-recursive" if rule.left_recursive else ""
            self.print("%s_type = %d%s" % (rulename, i, comment))
        self.print()
        self.print()
        self.print("class GeneratedParser(Parser):")
        with self.indent():
            self.print("KEYWORDS = KEYWORDS")
            self.print("SOFT_KEYWORDS = SOFT_KEYWORDS")
            self.print()
            self.print("def parse(self):")
            with self.indent():
                self.print("if self.start_rule == self.FILE_INPUT:")
                self.print("    return self._r_file()")
                self.print("elif self.start_rule == self.SINGLE_INPUT:")
                self.print("    return self._r_interactive()")
                self.print("elif self.start_rule == self.EVAL_INPUT:")
                self.print("    return self._r_eval()")
                self.print("elif self.start_rule == self.FUNC_TYPE_INPUT:")
                self.print("    return self._r_func_type()")
                self.print("return None")
            for rulename, rule in list(self.all_rules.items()):
                self.print()
                if rule.left_recursive:
                    self.print("# Left-recursive")
                self.visit(rule)

    # -- helpers --------------------------------------------------------

    def _set_up_token_start_metadata_extraction(self):
        self.print("if self.mark == self.fill:")
        with self.indent():
            self.print("self.fill_token()")
        self.print("_start_lineno = self.tokens[_mark].lineno")
        self.print("_start_col_offset = self.tokens[_mark].col_offset")

    def _set_up_token_end_metadata_extraction(self):
        self.print("_token = self.get_last_nonnwhitespace_token()")
        self.print("if _token is None:")
        with self.indent():
            self.add_return("None")
        self.print("_end_lineno = _token.end_lineno")
        self.print("_end_col_offset = _token.end_col_offset")

    def add_return(self, val):
        self.print("return %s" % val)

    def _should_memoize(self, node):
        return node.memo and not node.left_recursive

    def visit_Rule(self, node):
        is_loop = node.is_loop()
        is_gather = node.is_gather()
        rhs = node.flatten()
        for line in str(node).splitlines():
            self.print("# %s" % line)
        if node.left_recursive and node.leader:
            self._set_up_rule_memoization(node)
            self.print("def _r_%s_raw(self):" % node.name)
        else:
            self.print("def _r_%s(self):" % node.name)
        with self.indent():
            if node.name.endswith("without_invalid"):
                self.print("_prev_call_invalid = self.call_invalid_rules")
                self.print("self.call_invalid_rules = 0")
                self.print("try:")
                self.level += 1
                self.without_invalid_depth += 1
            if is_loop:
                self._handle_loop_rule_body(node, rhs)
            else:
                self._handle_default_rule_body(node, rhs)
            if node.name.endswith("without_invalid"):
                self.level -= 1
                self.print("finally:")
                with self.indent():
                    self.print("self.call_invalid_rules = _prev_call_invalid")
                self.without_invalid_depth -= 1

    def _set_up_rule_memoization(self, node):
        self.print("def _r_%s(self):" % node.name)
        with self.indent():
            self.print("_res = self.is_memoized(%s_type)" % node.name)
            self.print("if _res is not _NO_MEMO:")
            with self.indent():
                self.print("return _res")
            self.print("_res = None")
            self.print("_mark = self.mark")
            self.print("_resmark = self.mark")
            self.print("while True:")
            with self.indent():
                self.print("self.update_memo(_mark, %s_type, _res)" % node.name)
                self.print("self.mark = _mark")
                self.print("_raw = self._r_%s_raw()" % node.name)
                self.print("if _raw is None or self.mark <= _resmark:")
                with self.indent():
                    self.print("break")
                self.print("_resmark = self.mark")
                self.print("_res = _raw")
            self.print("self.mark = _resmark")
            self.print("return _res")
        self.print()

    def _handle_default_rule_body(self, node, rhs):
        memoize = self._should_memoize(node)
        self.print("_res = None")
        if memoize:
            self.print("_res = self.is_memoized(%s_type)" % node.name)
            self.print("if _res is not _NO_MEMO:")
            with self.indent():
                self.print("return _res")
        self.print("_mark = self.mark")
        if any(alt.action and "EXTRA" in alt.action for alt in rhs.alts):
            self._set_up_token_start_metadata_extraction()
        self.print("while True:")
        with self.indent():
            self.visit(rhs, is_loop=False, is_gather=node.is_gather(), rulename=node.name)
            self.print("_res = None")
            self.print("break")
        if memoize:
            self.print("self.insert_memo(_mark, %s_type, _res)" % node.name)
        self.print("return _res")

    def _handle_loop_rule_body(self, node, rhs):
        memoize = self._should_memoize(node)
        is_repeat1 = node.name.startswith("_loop1")
        self.print("_res = None")
        if memoize:
            self.print("_res = self.is_memoized(%s_type)" % node.name)
            self.print("if _res is not _NO_MEMO:")
            with self.indent():
                self.print("return _res")
        self.print("_mark = self.mark")
        if memoize:
            self.print("_start_mark = self.mark")
        self.print("_children = []")
        if any(alt.action and "EXTRA" in alt.action for alt in rhs.alts):
            self._set_up_token_start_metadata_extraction()
        self.visit(rhs, is_loop=True, is_gather=node.is_gather(), rulename=node.name)
        if is_repeat1:
            self.print("if not _children:")
            with self.indent():
                self.print("return None")
        self.print("_seq = _children")
        if memoize and node.name:
            self.print("self.insert_memo(_start_mark, %s_type, _seq)" % node.name)
        self.print("return _seq")

    def visit_Rhs(self, node, is_loop, is_gather, rulename):
        if is_loop:
            assert len(node.alts) == 1
        for alt in node.alts:
            self.visit(alt, is_loop=is_loop, is_gather=is_gather, rulename=rulename)

    def join_conditions(self, keyword, node):
        self.print("%s (" % keyword)
        with self.indent():
            first = True
            for item in node.items:
                call = self.callmakervisitor.generate_call(item)
                if call.assigned_variable:
                    call.assigned_variable = self.dedupe(call.assigned_variable)
                text = call.condition()
                if call.comment:
                    text += "  # %s" % call.comment
                self.print(("" if first else "and ") + text)
                first = False
        self.print("):")

    def emit_action(self, node):
        self.print("_res = %s" % translate_action(node.action))

    def emit_default_action(self, is_gather, node):
        names = self.local_variable_names
        if len(names) > 1:
            if is_gather:
                assert len(names) == 2
                self.print("_res = self.seq_insert_in_front(%s, %s)" % (names[0], names[1]))
            else:
                self.print("_res = self.dummy_name(%s)" % ", ".join(names))
        else:
            self.print("_res = %s" % names[0])

    def handle_alt_normal(self, node, is_gather, rulename):
        self.join_conditions("if", node)
        with self.indent():
            if node.action and "EXTRA" in node.action:
                self._set_up_token_end_metadata_extraction()
            if node.action:
                self.emit_action(node)
            else:
                self.emit_default_action(is_gather, node)
            self.print("break")

    def handle_alt_loop(self, node, is_gather, rulename):
        self.join_conditions("while", node)
        with self.indent():
            if node.action and "EXTRA" in node.action:
                self._set_up_token_end_metadata_extraction()
            if node.action:
                self.emit_action(node)
            else:
                self.emit_default_action(is_gather, node)
            self.print("_children.append(_res)")
            self.print("_mark = self.mark")

    def visit_Alt(self, node, is_loop, is_gather, rulename):
        invalid = len(node.items) == 1 and str(node.items[0]).startswith("invalid_")
        self.print("# %s" % str(node).replace("\n", " "))
        if invalid:
            self.print("if self.call_invalid_rules:")
            self.level += 1
        vars = self.collect_vars(node)
        if "_cut_var" in vars:
            self.print("_cut_var = False")
        with self.local_variable_context():
            if is_loop:
                self.handle_alt_loop(node, is_gather, rulename)
            else:
                self.handle_alt_normal(node, is_gather, rulename)
        self.print("self.mark = _mark")
        if "_cut_var" in vars:
            self.print("if _cut_var:")
            with self.indent():
                self.print("return None")
        if invalid:
            self.level -= 1

    def collect_vars(self, node):
        types = {}
        with self.local_variable_context():
            for item in node.items:
                name = self.add_var(item)
                types[name] = None
        return types

    def add_var(self, node):
        call = self.callmakervisitor.generate_call(node.item)
        name = node.name if node.name else call.assigned_variable
        if name is not None:
            name = self.dedupe(name)
        return name


def main():
    grammar_file = sys.argv[1]
    with open(grammar_file) as f:
        tokenizer = Tokenizer(tokenize.generate_tokens(f.readline))
        parser = GrammarParser(tokenizer)
        grammar = parser.start()
    if not grammar:
        raise SystemExit("failed to parse grammar")
    gen = PyParserGenerator(grammar, sys.stdout)
    gen.generate(grammar_file)


if __name__ == '__main__':
    main()
