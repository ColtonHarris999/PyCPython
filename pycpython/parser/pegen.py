"""Runtime for the generated PEG parser: port of Parser/pegen.c,
Parser/pegen_errors.c and Parser/action_helpers.c.

Error handling strategy: where the C code sets ``p->error_indicator`` and
returns NULL, we raise the corresponding Python exception directly.  Since
the C generated code unwinds immediately once ``error_indicator`` is set,
the two are equivalent.
"""

import os
import sys
import unicodedata

from ..marshal_writer import intern_identifier

from .. import tokens as T
from .. import pyast as ast
from ..tokenizer import (TokState, Token, E_OK, E_EOF, E_TOKEN, E_DEDENT, E_INTR,
                         E_NOMEM, E_TABSPACE, E_TOODEEP, E_LINECONT, E_DECODE,
                         E_COLUMNOVERFLOW, E_DONE, E_EOFS, E_EOLS, E_BADSINGLE,
                         E_ERROR, IUNDERFLOW_STOP, TSTRING,
                         byte_offset_to_character_offset)
from . import string_parser

_NO_MEMO = object()

STAR_TARGETS = 0
DEL_TARGETS = 1
FOR_TARGETS = 2

# Parser flags (pegen.h)
PyPARSE_DONT_IMPLY_DEDENT = 0x0002
PyPARSE_IGNORE_COOKIE = 0x0010
PyPARSE_BARRY_AS_BDFL = 0x0020
PyPARSE_TYPE_COMMENTS = 0x0040
PyPARSE_ALLOW_INCOMPLETE_INPUT = 0x0100

CURRENT_POS = -5

# Start symbols (Include/internal/pycore_pyarena.h / graminit)
Py_single_input = 256
Py_file_input = 257
Py_eval_input = 258
Py_func_type_input = 345


class IncompleteInputError(SyntaxError):
    """Mirror of _IncompleteInputError (PyExc_IncompleteInputError)."""
    __name__ = '_IncompleteInputError'


class PToken:
    """Parser-level token (struct Token in pegen.h)."""
    __slots__ = ('type', 'bytes', 'level', 'lineno', 'col_offset', 'end_lineno',
                 'end_col_offset', 'memo', 'metadata')

    def __init__(self):
        self.memo = {}
        self.metadata = None

    @property
    def string(self):
        return self.bytes.decode('utf-8', 'surrogateescape')

    def __repr__(self):
        return "<PToken %s %r %d:%d-%d:%d>" % (
            T.tok_name.get(self.type, self.type), self.bytes, self.lineno,
            self.col_offset, self.end_lineno, self.end_col_offset)


class CmpopExprPair:
    __slots__ = ('cmpop', 'expr')

    def __init__(self, cmpop, expr):
        self.cmpop = cmpop
        self.expr = expr


class KeyValuePair:
    __slots__ = ('key', 'value')

    def __init__(self, key, value):
        self.key = key
        self.value = value


class KeyPatternPair:
    __slots__ = ('key', 'pattern')

    def __init__(self, key, pattern):
        self.key = key
        self.pattern = pattern


class NameDefaultPair:
    __slots__ = ('arg', 'value')

    def __init__(self, arg, value):
        self.arg = arg
        self.value = value


class SlashWithDefault:
    __slots__ = ('plain_names', 'names_with_defaults')

    def __init__(self, plain_names, names_with_defaults):
        self.plain_names = plain_names
        self.names_with_defaults = names_with_defaults


class StarEtc:
    __slots__ = ('vararg', 'kwonlyargs', 'kwarg')

    def __init__(self, vararg, kwonlyargs, kwarg):
        self.vararg = vararg
        self.kwonlyargs = kwonlyargs
        self.kwarg = kwarg


class AugOperator:
    __slots__ = ('kind',)

    def __init__(self, kind):
        self.kind = kind


class KeywordOrStarred:
    __slots__ = ('element', 'is_keyword')

    def __init__(self, element, is_keyword):
        self.element = element
        self.is_keyword = is_keyword


class ResultTokenWithMetadata:
    __slots__ = ('result', 'metadata')

    def __init__(self, result, metadata):
        self.result = result
        self.metadata = metadata


_DUMMY_NAME = ast.Name('', ast.Load_singleton, 1, 0, 1, 0)


def pyformat(fmt, *args):
    """Minimal PyUnicode_FromFormat emulation for the formats used by the
    parser (%s %d %i %c %U %R %S %.3s %%)."""
    out = []
    i = 0
    n = len(fmt)
    ai = 0
    while i < n:
        c = fmt[i]
        if c != '%':
            out.append(c)
            i += 1
            continue
        i += 1
        prec = None
        if i < n and fmt[i] == '.':
            j = i + 1
            while j < n and fmt[j].isdigit():
                j += 1
            prec = int(fmt[i + 1:j])
            i = j
        spec = fmt[i]
        i += 1
        if spec == '%':
            out.append('%')
            continue
        arg = args[ai]
        ai += 1
        if spec in ('s', 'U', 'S'):
            s = arg if isinstance(arg, str) else str(arg)
            if prec is not None:
                s = s[:prec]
            out.append(s)
        elif spec in ('d', 'i', 'z', 'l'):
            out.append(str(int(arg)))
        elif spec == 'c':
            out.append(arg if isinstance(arg, str) else chr(arg))
        elif spec == 'R':
            out.append(repr(arg))
        else:
            raise ValueError("unsupported format %r" % spec)
    return ''.join(out)


def _program_text(filename, lineno, encoding):
    """Port of _PyErr_ProgramDecodedTextObject: read line *lineno* of the
    file on disk, if it exists."""
    if not filename or lineno <= 0 or not isinstance(filename, str):
        return None
    try:
        with open(filename, 'rb') as f:
            data = f.read()
    except OSError:
        return None
    if encoding is None:
        encoding = _find_encoding(data) or 'utf-8'
    # emulate _Py_UniversalNewlineFgetsWithSize with a 1000 byte buffer
    pos = 0
    n = len(data)
    line = None
    i = 0
    while i < lineno:
        if pos >= n:
            return None
        buf = bytearray()
        while pos < n and len(buf) < 999:
            c = data[pos]
            pos += 1
            if c == 13:
                if pos < n and data[pos] == 10:
                    pos += 1
                buf.append(10)
                break
            buf.append(c)
            if c == 10:
                break
        if i + 1 < lineno and len(buf) == 999 and buf[-1] != 10:
            continue
        line = bytes(buf)
        i += 1
    if line is None:
        return None
    if lineno == 1 and line[:3] == b'\xef\xbb\xbf':
        line = line[3:]
    try:
        return line.decode(encoding, 'replace')
    except LookupError:
        return None


def _find_encoding(data):
    from ..tokenizer import _get_coding_spec
    if data[:3] == b'\xef\xbb\xbf':
        return 'utf-8'
    lines = data.split(b'\n', 2)
    for ln in lines[:2]:
        enc = _get_coding_spec(ln + b'\n')
        if enc:
            return enc
        stripped = ln.strip(b' \t\014')
        if stripped and not stripped.startswith(b'#'):
            break
    return None


class Parser:
    """Base class for the generated parser (struct Parser in pegen.h)."""

    FILE_INPUT = Py_file_input
    SINGLE_INPUT = Py_single_input
    EVAL_INPUT = Py_eval_input
    FUNC_TYPE_INPUT = Py_func_type_input

    KEYWORDS = {}
    SOFT_KEYWORDS = []

    def __init__(self, tok, start_rule, flags, feature_version, filename):
        tok.type_comments = 1 if (flags & PyPARSE_TYPE_COMMENTS) else 0
        self.tok = tok
        self.tokens = []
        self.mark = 0
        self.fill = 0
        self.start_rule = start_rule
        self.parsing_started = 0
        self.error_indicator = 0
        self.starting_lineno = 0
        self.starting_col_offset = 0
        self.flags = flags
        self.feature_version = feature_version
        self.type_ignore_comments = []
        self.known_err_token = None
        self.level = 0
        self.call_invalid_rules = 0
        self.last_stmt_location = (0, 0, 0, 0)
        self.filename = filename
        self.errcode = None

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _keyword_or_name_type(self, pt, raw):
        try:
            s = raw.decode('utf-8')
        except UnicodeDecodeError:
            return T.NAME
        return self.KEYWORDS.get(s, T.NAME)

    def fill_token(self):
        tok = self.tok
        t = Token()
        type_ = tok.get(t)
        while type_ == T.TYPE_IGNORE:
            tag = tok.token_bytes(t)
            self.type_ignore_comments.append((tok.lineno, tag))
            t = Token()
            type_ = tok.get(t)
        if (self.start_rule == Py_single_input and type_ == T.ENDMARKER
                and self.parsing_started):
            type_ = T.NEWLINE
            self.parsing_started = 0
            if tok.indent and not (self.flags & PyPARSE_DONT_IMPLY_DEDENT):
                tok.pendin = -tok.indent
                tok.indent = 0
        else:
            self.parsing_started = 1
        pt = PToken()
        raw = tok.token_bytes(t)
        pt.type = self._keyword_or_name_type(pt, raw) if type_ == T.NAME else type_
        pt.bytes = raw
        pt.metadata = t.metadata
        pt.level = t.level
        pt.lineno = t.lineno
        pt.col_offset = t.col_offset
        pt.end_lineno = t.end_lineno
        pt.end_col_offset = t.end_col_offset
        self.tokens.append(pt)
        self.fill += 1
        if type_ == T.ERRORTOKEN and tok.done == E_DECODE:
            self.error_indicator = 1
            self.raise_decode_error()
        if type_ == T.ERRORTOKEN:
            self.error_indicator = 1
            self.tokenizer_error()
        return 0

    def raise_decode_error(self):
        exc = self.tok.exc
        if isinstance(exc, UnicodeError):
            errtype = "unicode error"
        elif isinstance(exc, ValueError):
            errtype = "value error"
        else:
            errtype = None
        if errtype:
            self.raise_syntax_error("(%s) %U", errtype, str(exc))
        raise exc

    def raise_unclosed_parentheses_error(self):
        tok = self.tok
        error_lineno = tok.parenlinenostack[tok.level - 1]
        error_col = tok.parencolstack[tok.level - 1]
        self.raise_error_known_location(SyntaxError, error_lineno, error_col,
                                        error_lineno, -1, "'%c' was never closed",
                                        tok.parenstack[tok.level - 1])

    def tokenizer_error(self):
        tok = self.tok
        if tok.exc is not None:
            raise tok.exc
        errtype = SyntaxError
        col_offset = -1
        self.error_indicator = 1
        done = tok.done
        if done == E_TOKEN:
            msg = "invalid token"
        elif done == E_EOF:
            if tok.level:
                self.raise_unclosed_parentheses_error()
            else:
                self.raise_syntax_error("unexpected EOF while parsing")
            return -1
        elif done == E_DEDENT:
            self.raise_indentation_error("unindent does not match any outer indentation level")
            return -1
        elif done == E_INTR:
            raise KeyboardInterrupt
        elif done == E_NOMEM:
            raise MemoryError
        elif done == E_TABSPACE:
            errtype = TabError
            msg = "inconsistent use of tabs and spaces in indentation"
        elif done == E_TOODEEP:
            errtype = IndentationError
            msg = "too many levels of indentation"
        elif done == E_LINECONT:
            col_offset = tok.cur - tok.buf - 1
            msg = "unexpected character after line continuation character"
        elif done == E_COLUMNOVERFLOW:
            raise OverflowError("Parser column offset overflow - source line is too big")
        else:
            msg = "unknown parsing error"
        self.raise_error_known_location(errtype, tok.lineno,
                                        col_offset if col_offset >= 0 else 0,
                                        tok.lineno, -1, msg)
        return -1

    def is_memoized(self, rule_type):
        if self.mark == self.fill:
            self.fill_token()
        t = self.tokens[self.mark]
        m = t.memo.get(rule_type)
        if m is None:
            return _NO_MEMO
        self.mark = m[1]
        return m[0]

    def insert_memo(self, mark, rule_type, node):
        self.tokens[mark].memo[rule_type] = (node, self.mark)

    def update_memo(self, mark, rule_type, node):
        self.tokens[mark].memo[rule_type] = (node, self.mark)

    def lookahead(self, positive, func, *args):
        mark = self.mark
        res = func(*args)
        self.mark = mark
        return (res is not None) == positive

    def expect_token(self, type_):
        if self.mark == self.fill:
            self.fill_token()
        t = self.tokens[self.mark]
        if t.type != type_:
            return None
        self.mark += 1
        return t

    def expect_forced_result(self, result, expected):
        if result is None:
            self.raise_syntax_error("expected (%s)", expected)
        return result

    def expect_forced_token(self, type_, expected):
        if self.mark == self.fill:
            self.fill_token()
        t = self.tokens[self.mark]
        if t.type != type_:
            self.raise_syntax_error_known_location(t, "expected '%s'", expected)
        self.mark += 1
        return t

    def expect_soft_keyword(self, keyword):
        if self.mark == self.fill:
            self.fill_token()
        t = self.tokens[self.mark]
        if t.type != T.NAME:
            return None
        if t.bytes != keyword.encode('utf-8'):
            return None
        return self.name_token()

    def get_last_nonnwhitespace_token(self):
        token = None
        for m in range(self.mark - 1, -1, -1):
            token = self.tokens[m]
            if token.type != T.ENDMARKER and (token.type < T.NEWLINE or token.type > T.DEDENT):
                break
        return token

    def new_identifier(self, n):
        try:
            id_ = n.decode('utf-8') if isinstance(n, bytes) else n
        except UnicodeDecodeError:
            self.error_indicator = 1
            raise
        if not id_.isascii():
            id_ = unicodedata.normalize('NFKC', id_)
        if id_ in ('None', 'True', 'False'):
            self.error_indicator = 1
            raise ValueError("identifier field can't represent '%s' constant" % id_)
        return intern_identifier(id_)

    def _name_from_token(self, t):
        if t is None:
            return None
        id_ = self.new_identifier(t.bytes)
        return ast.Name(id_, ast.Load_singleton, t.lineno, t.col_offset,
                        t.end_lineno, t.end_col_offset)

    def name_token(self):
        t = self.expect_token(T.NAME)
        return self._name_from_token(t)

    def string_token(self):
        return self.expect_token(T.STRING)

    def soft_keyword_token(self):
        t = self.expect_token(T.NAME)
        if t is None:
            return None
        s = t.string
        for keyword in self.SOFT_KEYWORDS:
            if keyword == s:
                return self._name_from_token(t)
        return None

    @staticmethod
    def _parsenumber(s):
        if '_' in s:
            s = s.replace('_', '')
        imflag = s[-1] in 'jJ'
        if imflag:
            return complex(0.0, float(s[:-1]))
        try:
            if s[0] == '0':
                return int(s, 0)
            return int(s, 10)
        except ValueError as e:
            if 'Exceeds the limit' in str(e) or 'too many digits' in str(e):
                raise
        return float(s)

    def number_token(self):
        t = self.expect_token(T.NUMBER)
        if t is None:
            return None
        num_raw = t.string
        if self.feature_version < 6 and '_' in num_raw:
            self.error_indicator = 1
            self.raise_syntax_error("Underscores in numeric literals are only supported "
                                    "in Python 3.6 and greater")
        try:
            c = self._parsenumber(num_raw)
        except ValueError as exc:
            self.error_indicator = 1
            self.raise_error_known_location(
                SyntaxError, t.lineno, -1, t.end_lineno, -1,
                "%S - Consider hexadecimal for huge integer literals "
                "to avoid decimal conversion limits.", exc)
        return ast.Constant(c, None, t.lineno, t.col_offset, t.end_lineno, t.end_col_offset)

    # ------------------------------------------------------------------
    # Errors (pegen_errors.c)
    # ------------------------------------------------------------------

    def raise_error(self, errtype, use_mark, errmsg, *args):
        if self.fill == 0:
            self._raise_error_known_location(errtype, 0, 0, 0, -1, errmsg, args)
        if use_mark and self.mark == self.fill:
            self.fill_token()
        if self.known_err_token is not None:
            t = self.known_err_token
        else:
            t = self.tokens[self.mark if use_mark else self.fill - 1]
        tok = self.tok
        end_col_offset = -1
        if t.col_offset == -1:
            if tok.cur == tok.buf:
                col_offset = 0
            else:
                col_offset = tok.cur - tok.line_start
        else:
            col_offset = t.col_offset + 1
        if t.end_col_offset != -1:
            end_col_offset = t.end_col_offset + 1
        self._raise_error_known_location(errtype, t.lineno, col_offset, t.end_lineno,
                                         end_col_offset, errmsg, args)

    def raise_error_known_location(self, errtype, lineno, col_offset, end_lineno,
                                   end_col_offset, errmsg, *args):
        """RAISE_ERROR_KNOWN_LOCATION macro (adds 1 to the columns)."""
        _col_offset = CURRENT_POS if col_offset == CURRENT_POS else col_offset + 1
        _end_col_offset = CURRENT_POS if end_col_offset == CURRENT_POS else end_col_offset + 1
        self._raise_error_known_location(errtype, lineno, _col_offset, end_lineno,
                                         _end_col_offset, errmsg, args)

    def _get_error_line_from_tokenizer_buffers(self, lineno):
        tok = self.tok
        data = tok.data
        cur_line = 0
        buf_end = tok.inp
        relative_lineno = lineno
        for i in range(relative_lineno - 1):
            new_line = data.find(b'\n', cur_line)
            if new_line < 0 or new_line + 1 > buf_end:
                break
            cur_line = new_line + 1
        next_newline = data.find(b'\n', cur_line)
        if next_newline < 0:
            next_newline = len(data)
        return bytes(data[cur_line:next_newline]).decode('utf-8', 'replace')

    def _raise_error_known_location(self, errtype, lineno, col_offset, end_lineno,
                                    end_col_offset, errmsg, args):
        tok = self.tok
        self.error_indicator = 1
        if end_lineno == CURRENT_POS:
            end_lineno = tok.lineno
        if end_col_offset == CURRENT_POS:
            end_col_offset = tok.cur - tok.line_start
        errstr = pyformat(errmsg, *args)
        error_line = None
        if self.start_rule == Py_file_input:
            error_line = _program_text(tok.filename, lineno, tok.encoding)
        if error_line is None:
            if tok.lineno <= lineno and tok.inp > tok.buf:
                error_line = bytes(tok.data[tok.line_start:tok.inp]).decode('utf-8', 'replace')
            else:
                error_line = self._get_error_line_from_tokenizer_buffers(lineno)
        line_utf8 = error_line.encode('utf-8', 'surrogatepass')
        col_number = byte_offset_to_character_offset(line_utf8, col_offset)
        end_col_number = end_col_offset
        if end_col_offset > 0:
            end_col_number = byte_offset_to_character_offset(line_utf8, end_col_offset)
        raise errtype(errstr, (tok.filename, lineno, col_number, error_line,
                               end_lineno, end_col_number))

    def raise_syntax_error(self, msg, *args):
        self.raise_error(SyntaxError, 0, msg, *args)

    def raise_indentation_error(self, msg, *args):
        self.raise_error(IndentationError, 0, msg, *args)

    def raise_syntax_error_on_next_token(self, msg, *args):
        self.raise_error(SyntaxError, 1, msg, *args)

    def raise_syntax_error_known_range(self, a, b, msg, *args):
        self.raise_error_known_location(SyntaxError, a.lineno, a.col_offset,
                                        b.end_lineno, b.end_col_offset, msg, *args)

    def raise_syntax_error_known_location(self, a, msg, *args):
        self.raise_error_known_location(SyntaxError, a.lineno, a.col_offset,
                                        a.end_lineno, a.end_col_offset, msg, *args)

    def raise_syntax_error_starting_from(self, a, msg, *args):
        self.raise_error_known_location(SyntaxError, a.lineno, a.col_offset,
                                        CURRENT_POS, CURRENT_POS, msg, *args)

    def raise_syntax_error_invalid_target(self, targets_type, e):
        invalid_target = self.get_invalid_target(e, targets_type)
        if invalid_target is not None:
            if targets_type == STAR_TARGETS or targets_type == FOR_TARGETS:
                msg = "cannot assign to %s"
            else:
                msg = "cannot delete %s"
            self.raise_syntax_error_known_location(invalid_target, msg,
                                                   self.get_expr_name(invalid_target))
        return None

    def check_version(self, version, msg, node):
        if node is None:
            self.error_indicator = 1
            return None
        if self.feature_version < version:
            self.error_indicator = 1
            self.raise_syntax_error("%s only supported in Python 3.%i and greater",
                                    msg, version)
        return node

    def new_type_comment(self, tc):
        if tc is None:
            return None
        return tc.bytes.decode('utf-8')

    # ------------------------------------------------------------------
    # Driver (pegen.c)
    # ------------------------------------------------------------------

    def interactive_exit(self):
        self.errcode = E_EOF
        return None

    def _is_end_of_source(self):
        err = self.tok.done
        return err == E_EOF or err == E_EOFS or err == E_EOLS

    def _reset_parser_state_for_error_pass(self):
        self.last_stmt_location = (0, 0, 0, 0)
        for i in range(self.fill):
            self.tokens[i].memo = {}
        self.mark = 0
        self.call_invalid_rules = 1
        self.tok.interactive_underflow = IUNDERFLOW_STOP

    def _bad_single_statement(self):
        tok = self.tok
        data = tok.data
        cur = tok.cur
        n = len(data)
        while True:
            while cur < n and data[cur] in (32, 9, 10, 12):
                cur += 1
            if cur >= n or data[cur] == 0:
                return False
            if data[cur] != 35:
                return True
            while cur < n and data[cur] != 0 and data[cur] != 10:
                cur += 1

    def _tokenize_full_source_to_check_for_errors(self, pending):
        """Port of _PyPegen_tokenize_full_source_to_check_for_errors.

        *pending* is the exception currently "set".  Returns the exception
        that should finally be raised."""
        tok = self.tok
        if tok.prompt is not None:
            return pending
        current_token = self.known_err_token if self.known_err_token is not None \
            else self.tokens[self.fill - 1]
        current_err_line = current_token.lineno
        new_exc = None
        while True:
            t = Token()
            typ = tok.get(t)
            if typ == T.ERRORTOKEN:
                if tok.exc is not None:
                    new_exc = tok.exc
                    break
                if tok.level != 0:
                    error_lineno = tok.parenlinenostack[tok.level - 1]
                    if current_err_line > error_lineno:
                        try:
                            self.raise_unclosed_parentheses_error()
                        except SyntaxError as e:
                            new_exc = e
                        break
                break
            elif typ == T.ENDMARKER:
                break
            else:
                continue
        if new_exc is not None and tok.tok_mode_stack_index <= 0:
            return new_exc
        return pending

    def _set_syntax_error(self, last_token, pending):
        """Port of _Pypegen_set_syntax_error.  Returns the exception to raise."""
        tok = self.tok
        if pending is not None:
            is_tok_ok = tok.done == E_DONE or tok.done == E_OK
            if is_tok_ok and isinstance(pending, SyntaxError):
                return self._tokenize_full_source_to_check_for_errors(pending)
            return pending
        try:
            if self.fill == 0:
                self.raise_syntax_error("error at start before reading any input")
            if last_token.type == T.ERRORTOKEN and tok.done == E_EOF:
                if tok.level:
                    self.raise_unclosed_parentheses_error()
                else:
                    self.raise_syntax_error("unexpected EOF while parsing")
            if last_token.type == T.INDENT or last_token.type == T.DEDENT:
                self.raise_indentation_error(
                    "unexpected indent" if last_token.type == T.INDENT
                    else "unexpected unindent")
            self.raise_syntax_error_known_location(last_token, "invalid syntax")
        except SyntaxError as e:
            generic = e
        if isinstance(generic, SyntaxError) and type(generic) is SyntaxError:
            return self._tokenize_full_source_to_check_for_errors(generic)
        return generic

    def _set_syntax_error_metadata(self, exc):
        if not isinstance(exc, SyntaxError):
            return
        tok = self.tok
        source = None
        if tok.str is not None:
            try:
                source = bytes(tok.str).decode(tok.encoding or 'utf-8')
            except (UnicodeDecodeError, LookupError):
                source = None
        exc._metadata = (self.last_stmt_location[0], self.last_stmt_location[1], source)

    def run_parser(self):
        """_PyPegen_run_parser."""
        pending = None
        res = None
        try:
            res = self.parse()
        except RecursionError:
            raise MemoryError("Parser stack overflowed - Python source too complex to parse")
        except (SyntaxError, ValueError, UnicodeError, MemoryError, OverflowError,
                KeyboardInterrupt) as e:
            pending = e
        if res is None:
            if (self.flags & PyPARSE_ALLOW_INCOMPLETE_INPUT) and self._is_end_of_source():
                self.raise_error(IncompleteInputError, 0, "incomplete input")
            if pending is not None and not isinstance(pending, SyntaxError):
                raise pending
            last_token = self.tokens[self.fill - 1]
            self._reset_parser_state_for_error_pass()
            if pending is None:
                try:
                    self.parse()
                except RecursionError:
                    raise MemoryError("Parser stack overflowed - Python source too complex to parse")
                except (SyntaxError, ValueError, UnicodeError, MemoryError, OverflowError,
                        KeyboardInterrupt) as e:
                    pending = e
                    if not isinstance(e, SyntaxError):
                        raise
            exc = self._set_syntax_error(last_token, pending)
            if isinstance(exc, SyntaxError):
                self._set_syntax_error_metadata(exc)
            raise exc
        if self.start_rule == Py_single_input and self._bad_single_statement():
            self.tok.done = E_BADSINGLE
            self.raise_syntax_error("multiple statements found while compiling a single statement")
        return res

    # ------------------------------------------------------------------
    # Action helpers (action_helpers.c)
    # ------------------------------------------------------------------

    def dummy_name(self, *args):
        return _DUMMY_NAME

    def singleton_seq(self, a):
        return [a]

    def seq_insert_in_front(self, a, seq):
        if seq is None:
            return [a]
        return [a] + list(seq)

    def seq_append_to_end(self, seq, a):
        if seq is None:
            return [a]
        return list(seq) + [a]

    def seq_flatten(self, seqs):
        out = []
        for inner in seqs or ():
            out.extend(inner)
        return out

    def join_names_with_dot(self, first_name, second_name):
        uni = "%s.%s" % (first_name.id, second_name.id)
        return ast.Name(uni, ast.Load_singleton, first_name.lineno, first_name.col_offset,
                        second_name.end_lineno, second_name.end_col_offset)

    def seq_count_dots(self, seq):
        n = 0
        for t in seq or ():
            if t.type == T.ELLIPSIS:
                n += 3
            elif t.type == T.DOT:
                n += 1
        return n

    def alias_for_star(self, lineno, col_offset, end_lineno, end_col_offset):
        return ast.alias('*', None, lineno, col_offset, end_lineno, end_col_offset)

    def map_names_to_ids(self, seq):
        return [e.id for e in seq or ()]

    def cmpop_expr_pair(self, cmpop, expr):
        return CmpopExprPair(cmpop, expr)

    def get_cmpops(self, seq):
        return [pair.cmpop for pair in seq or ()]

    def get_exprs(self, seq):
        return [pair.expr for pair in seq or ()]

    def _set_seq_context(self, seq, ctx):
        if len(seq) == 0:
            return None
        return [self.set_expr_context(e, ctx) for e in seq]

    def set_expr_context(self, expr, ctx):
        if isinstance(expr, ast.Name):
            return ast.Name(expr.id, ctx, expr.lineno, expr.col_offset,
                            expr.end_lineno, expr.end_col_offset)
        if isinstance(expr, ast.Tuple):
            return ast.Tuple(self._set_seq_context(expr.elts, ctx), ctx, expr.lineno,
                             expr.col_offset, expr.end_lineno, expr.end_col_offset)
        if isinstance(expr, ast.List):
            return ast.List(self._set_seq_context(expr.elts, ctx), ctx, expr.lineno,
                            expr.col_offset, expr.end_lineno, expr.end_col_offset)
        if isinstance(expr, ast.Subscript):
            return ast.Subscript(expr.value, expr.slice, ctx, expr.lineno,
                                 expr.col_offset, expr.end_lineno, expr.end_col_offset)
        if isinstance(expr, ast.Attribute):
            return ast.Attribute(expr.value, expr.attr, ctx, expr.lineno,
                                 expr.col_offset, expr.end_lineno, expr.end_col_offset)
        if isinstance(expr, ast.Starred):
            inner = self.set_expr_context(expr.value, ctx)
            return ast.Starred(inner, ctx, expr.lineno, expr.col_offset,
                               expr.end_lineno, expr.end_col_offset)
        return expr

    def key_value_pair(self, key, value):
        return KeyValuePair(key, value)

    def get_keys(self, seq):
        return [pair.key for pair in seq or ()]

    def get_values(self, seq):
        return [pair.value for pair in seq or ()]

    def key_pattern_pair(self, key, pattern):
        return KeyPatternPair(key, pattern)

    def get_pattern_keys(self, seq):
        return [pair.key for pair in seq or ()]

    def get_patterns(self, seq):
        return [pair.pattern for pair in seq or ()]

    def name_default_pair(self, arg, value, tc):
        return NameDefaultPair(self.add_type_comment_to_arg(arg, tc), value)

    def slash_with_default(self, plain_names, names_with_defaults):
        return SlashWithDefault(plain_names, names_with_defaults)

    def star_etc(self, vararg, kwonlyargs, kwarg):
        return StarEtc(vararg, kwonlyargs, kwarg)

    def join_sequences(self, a, b):
        return list(a or ()) + list(b or ())

    @staticmethod
    def _get_names(names_with_defaults):
        return [pair.arg for pair in names_with_defaults]

    @staticmethod
    def _get_defaults(names_with_defaults):
        return [pair.value for pair in names_with_defaults]

    def make_arguments(self, slash_without_default, slash_with_default, plain_names,
                       names_with_default, star_etc):
        if slash_without_default is not None:
            posonlyargs = slash_without_default
        elif slash_with_default is not None:
            posonlyargs = list(slash_with_default.plain_names) + \
                self._get_names(slash_with_default.names_with_defaults)
        else:
            posonlyargs = []
        if names_with_default is not None:
            if plain_names is not None:
                posargs = list(plain_names) + self._get_names(names_with_default)
            else:
                posargs = self._get_names(names_with_default)
        else:
            if plain_names is not None:
                posargs = plain_names
            else:
                posargs = []
        if slash_with_default is not None and names_with_default is not None:
            posdefaults = self._get_defaults(slash_with_default.names_with_defaults) + \
                self._get_defaults(names_with_default)
        elif slash_with_default is None and names_with_default is not None:
            posdefaults = self._get_defaults(names_with_default)
        elif slash_with_default is not None and names_with_default is None:
            posdefaults = self._get_defaults(slash_with_default.names_with_defaults)
        else:
            posdefaults = []
        vararg = None
        if star_etc is not None and star_etc.vararg is not None:
            vararg = star_etc.vararg
        if star_etc is not None and star_etc.kwonlyargs is not None:
            kwonlyargs = self._get_names(star_etc.kwonlyargs)
            kwdefaults = self._get_defaults(star_etc.kwonlyargs)
        else:
            kwonlyargs = []
            kwdefaults = []
        kwarg = None
        if star_etc is not None and star_etc.kwarg is not None:
            kwarg = star_etc.kwarg
        return ast.arguments(posonlyargs, posargs, vararg, kwonlyargs, kwdefaults,
                             kwarg, posdefaults)

    def empty_arguments(self):
        return ast.arguments([], [], None, [], [], None, [])

    def augoperator(self, kind):
        return AugOperator(kind)

    def function_def_decorators(self, decorators, function_def):
        if isinstance(function_def, ast.AsyncFunctionDef):
            return ast.AsyncFunctionDef(
                function_def.name, function_def.args, function_def.body, decorators,
                function_def.returns, function_def.type_comment, function_def.type_params,
                function_def.lineno, function_def.col_offset, function_def.end_lineno,
                function_def.end_col_offset)
        return ast.FunctionDef(
            function_def.name, function_def.args, function_def.body, decorators,
            function_def.returns, function_def.type_comment, function_def.type_params,
            function_def.lineno, function_def.col_offset, function_def.end_lineno,
            function_def.end_col_offset)

    def class_def_decorators(self, decorators, class_def):
        return ast.ClassDef(class_def.name, class_def.bases, class_def.keywords,
                            class_def.body, decorators, class_def.type_params,
                            class_def.lineno, class_def.col_offset, class_def.end_lineno,
                            class_def.end_col_offset)

    def keyword_or_starred(self, element, is_keyword):
        return KeywordOrStarred(element, is_keyword)

    def seq_extract_starred_exprs(self, kwargs):
        res = [k.element for k in kwargs or () if not k.is_keyword]
        if not res:
            return None
        return res

    def seq_delete_starred_exprs(self, kwargs):
        res = [k.element for k in kwargs or () if k.is_keyword]
        if not res:
            return None
        return res

    def ensure_imaginary(self, exp):
        if not isinstance(exp, ast.Constant) or type(exp.value) is not complex:
            self.raise_syntax_error_known_location(
                exp, "imaginary number required in complex literal")
        return exp

    def ensure_real(self, exp):
        if not isinstance(exp, ast.Constant) or type(exp.value) is complex:
            self.raise_syntax_error_known_location(
                exp, "real number required in complex literal")
        return exp

    def make_module(self, a):
        type_ignores = []
        for lineno, comment in self.type_ignore_comments:
            tag = comment.decode('utf-8')
            type_ignores.append(ast.TypeIgnore(lineno, tag))
        return ast.Module(a, type_ignores)

    def add_type_comment_to_arg(self, a, tc):
        if tc is None:
            return a
        tco = tc.bytes.decode('utf-8')
        return ast.arg(a.arg, a.annotation, tco, a.lineno, a.col_offset,
                       a.end_lineno, a.end_col_offset)

    def check_barry_as_flufl(self, t):
        tok_str = t.bytes
        if (self.flags & PyPARSE_BARRY_AS_BDFL) and tok_str != b"<>":
            self.raise_syntax_error("with Barry as BDFL, use '<>' instead of '!='")
            return -1
        if not (self.flags & PyPARSE_BARRY_AS_BDFL):
            return 1 if tok_str != b"!=" else 0
        return 0

    def check_legacy_stmt(self, name):
        if not isinstance(name, ast.Name):
            return 0
        return 1 if name.id in ("print", "exec") else 0

    def check_fstring_conversion(self, conv_token, conv):
        if conv_token.lineno != conv.lineno or conv_token.end_col_offset != conv.col_offset:
            self.raise_syntax_error_known_range(
                conv_token, conv,
                "%c-string: conversion type must come right after the exclamation mark",
                self.tok.string_prefix())
        first = conv.id[0]
        if len(conv.id) > 1 or first not in ('s', 'r', 'a'):
            self.raise_syntax_error_known_location(
                conv, "%c-string: invalid conversion character %R: expected 's', 'r', or 'a'",
                self.tok.string_prefix(), conv.id)
        return ResultTokenWithMetadata(conv, conv_token.metadata)

    @staticmethod
    def _is_empty_str_constant(item):
        return (isinstance(item, ast.Constant) and type(item.value) is str
                and len(item.value) == 0)

    def setup_full_format_spec(self, colon, spec, lineno, col_offset, end_lineno,
                               end_col_offset):
        if spec is None:
            return None
        non_empty = [item for item in spec if not self._is_empty_str_constant(item)]
        if len(non_empty) != len(spec):
            spec = non_empty
        n = len(spec)
        if n == 0 or (n == 1 and isinstance(spec[0], ast.Constant)):
            res = ast.JoinedStr(spec, lineno, col_offset, end_lineno, end_col_offset)
        else:
            res = self.concatenate_strings(spec, lineno, col_offset, end_lineno, end_col_offset)
        return ResultTokenWithMetadata(res, colon.metadata)

    def get_expr_name(self, e):
        k = type(e)
        if k is ast.Attribute:
            return "attribute"
        if k is ast.Subscript:
            return "subscript"
        if k is ast.Starred:
            return "starred"
        if k is ast.Name:
            return "name"
        if k is ast.List:
            return "list"
        if k is ast.Tuple:
            return "tuple"
        if k is ast.Lambda:
            return "lambda"
        if k is ast.Call:
            return "function call"
        if k in (ast.BoolOp, ast.BinOp, ast.UnaryOp):
            return "expression"
        if k is ast.GeneratorExp:
            return "generator expression"
        if k in (ast.Yield, ast.YieldFrom):
            return "yield expression"
        if k is ast.Await:
            return "await expression"
        if k is ast.ListComp:
            return "list comprehension"
        if k is ast.SetComp:
            return "set comprehension"
        if k is ast.DictComp:
            return "dict comprehension"
        if k is ast.Dict:
            return "dict literal"
        if k is ast.Set:
            return "set display"
        if k in (ast.JoinedStr, ast.FormattedValue):
            return "f-string expression"
        if k in (ast.TemplateStr, ast.Interpolation):
            return "t-string expression"
        if k is ast.Constant:
            v = e.value
            if v is None:
                return "None"
            if v is False:
                return "False"
            if v is True:
                return "True"
            if v is Ellipsis:
                return "ellipsis"
            return "literal"
        if k is ast.Compare:
            return "comparison"
        if k is ast.IfExp:
            return "conditional expression"
        if k is ast.NamedExpr:
            return "named expression"
        raise SystemError("unexpected expression in assignment %s (line %d)"
                          % (k.__name__, e.lineno))

    def get_last_comprehension_item(self, comprehension):
        if not comprehension.ifs:
            return comprehension.iter
        return comprehension.ifs[-1]

    def collect_call_seqs(self, a, b, lineno, col_offset, end_lineno, end_col_offset):
        if b is None:
            return ast.Call(_DUMMY_NAME, a, None, lineno, col_offset, end_lineno, end_col_offset)
        starreds = self.seq_extract_starred_exprs(b)
        keywords = self.seq_delete_starred_exprs(b)
        args = list(a)
        if starreds:
            args.extend(starreds)
        return ast.Call(_DUMMY_NAME, args, keywords, lineno, col_offset, end_lineno,
                        end_col_offset)

    def get_invalid_target(self, e, targets_type):
        if e is None:
            return None
        k = type(e)
        if k is ast.List or k is ast.Tuple:
            for other in e.elts:
                child = self.get_invalid_target(other, targets_type)
                if child is not None:
                    return child
            return None
        if k is ast.Starred:
            if targets_type == DEL_TARGETS:
                return e
            return self.get_invalid_target(e.value, targets_type)
        if k is ast.Compare:
            if targets_type == FOR_TARGETS:
                cmpop = e.ops[0]
                if cmpop is ast.In_singleton:
                    return self.get_invalid_target(e.left, targets_type)
                return None
            return e
        if k in (ast.Name, ast.Subscript, ast.Attribute):
            return None
        return e

    def arguments_parsing_error(self, e):
        kwarg_unpacking = 0
        for keyword in e.keywords:
            if keyword.arg is None:
                kwarg_unpacking = 1
        if kwarg_unpacking:
            msg = "positional argument follows keyword argument unpacking"
        else:
            msg = "positional argument follows keyword argument"
        self.raise_syntax_error(msg)

    def nonparen_genexp_in_call(self, args, comprehensions):
        n = len(args.args)
        if n <= 1:
            return None
        last_comprehension = comprehensions[-1]
        self.raise_syntax_error_known_range(
            args.args[n - 1], self.get_last_comprehension_item(last_comprehension),
            "Generator expression must be parenthesized")

    # -- f-string stuff --------------------------------------------------

    def _decode_fstring_part(self, is_raw, constant, token):
        bstr = constant.value.encode('utf-8', 'surrogatepass')
        if bstr == b"{{" or bstr == b"}}":
            bstr = bstr[:1]
        is_raw = is_raw or (92 not in bstr)
        try:
            s = string_parser.decode_string(self, is_raw, bstr, token)
        except string_parser.DecodeError as e:
            self.tok.exc = e.exc
            self.raise_decode_error()
        return ast.Constant(s, None, constant.lineno, constant.col_offset,
                            constant.end_lineno, constant.end_col_offset)

    def _get_resized_exprs(self, a, raw_expressions, b, string_kind):
        quote_str = a.bytes
        is_raw = (b'r' in quote_str) or (b'R' in quote_str)
        seq = []
        for item in raw_expressions:
            if isinstance(item, ast.JoinedStr):
                values = item.values
                if len(values) != 2:
                    raise SystemError(
                        ("unexpected TemplateStr node without debug data in t-string at line %d"
                         if string_kind == TSTRING else
                         "unexpected JoinedStr node without debug data in f-string at line %d")
                        % item.lineno)
                seq.append(values[0])
                seq.append(values[1])
                continue
            if isinstance(item, ast.Constant):
                item = self._decode_fstring_part(is_raw, item, b)
                if type(item.value) is str and len(item.value) == 0:
                    continue
            seq.append(item)
        return seq

    def template_str(self, a, raw_expressions, b):
        resized = self._get_resized_exprs(a, raw_expressions, b, TSTRING)
        return ast.TemplateStr(resized, a.lineno, a.col_offset, b.end_lineno, b.end_col_offset)

    def joined_str(self, a, raw_expressions, b):
        resized = self._get_resized_exprs(a, raw_expressions, b, 0)
        return ast.JoinedStr(resized, a.lineno, a.col_offset, b.end_lineno, b.end_col_offset)

    def decoded_constant_from_token(self, tok):
        bstr = tok.bytes
        is_raw = 0
        if self.tok.inside_fstring():
            is_raw = self.tok.mode().raw
        try:
            s = string_parser.decode_string(self, is_raw, bstr, tok)
        except string_parser.DecodeError as e:
            self.error_indicator = 1
            raise e.exc
        return ast.Constant(s, None, tok.lineno, tok.col_offset, tok.end_lineno,
                            tok.end_col_offset)

    def constant_from_token(self, tok):
        s = tok.bytes.decode('utf-8', 'surrogatepass')
        return ast.Constant(s, None, tok.lineno, tok.col_offset, tok.end_lineno,
                            tok.end_col_offset)

    def constant_from_string(self, tok):
        try:
            s = string_parser.parse_string(self, tok)
        except string_parser.DecodeError as e:
            self.tok.exc = e.exc
            self.raise_decode_error()
        kind = None
        if tok.bytes[:1] == b'u':
            kind = self.new_identifier(b"u")
        return ast.Constant(s, kind, tok.lineno, tok.col_offset, tok.end_lineno,
                            tok.end_col_offset)

    @staticmethod
    def _get_interpolation_conversion(debug, conversion, format):
        if conversion is not None:
            conversion_expr = conversion.result
            return ord(conversion_expr.id[0])
        elif debug is not None and format is None:
            return ord('r')
        return -1

    @staticmethod
    def _strip_interpolation_expr(exprstr):
        n = len(exprstr)
        for i in range(n - 1, -1, -1):
            c = exprstr[i]
            if c.isspace() or c == '=':
                n -= 1
            else:
                break
        return exprstr[:n]

    def interpolation(self, expression, debug, conversion, format, closing_brace,
                      lineno, col_offset, end_lineno, end_col_offset):
        conversion_val = self._get_interpolation_conversion(debug, conversion, format)
        if conversion is not None:
            debug_end_line = conversion.result.lineno
            debug_end_offset = conversion.result.col_offset
            debug_metadata = exprstr = conversion.metadata
        elif format is not None:
            debug_end_line = format.result.lineno
            debug_end_offset = format.result.col_offset + 1
            debug_metadata = exprstr = format.metadata
        else:
            debug_end_line = end_lineno
            debug_end_offset = end_col_offset
            debug_metadata = exprstr = closing_brace.metadata
        final_exprstr = self._strip_interpolation_expr(exprstr)
        interp = ast.Interpolation(expression, final_exprstr, conversion_val,
                                   format.result if format is not None else None,
                                   lineno, col_offset, end_lineno, end_col_offset)
        if debug is None:
            return interp
        debug_text = ast.Constant(debug_metadata, None, lineno, col_offset + 1,
                                  debug_end_line, debug_end_offset - 1)
        return ast.JoinedStr([debug_text, interp], lineno, col_offset,
                             debug_end_line, debug_end_offset)

    def formatted_value(self, expression, debug, conversion, format, closing_brace,
                        lineno, col_offset, end_lineno, end_col_offset):
        conversion_val = self._get_interpolation_conversion(debug, conversion, format)
        fv = ast.FormattedValue(expression, conversion_val,
                                format.result if format is not None else None,
                                lineno, col_offset, end_lineno, end_col_offset)
        if debug is None:
            return fv
        if conversion is not None:
            debug_end_line = conversion.result.lineno
            debug_end_offset = conversion.result.col_offset
            debug_metadata = conversion.metadata
        elif format is not None:
            debug_end_line = format.result.lineno
            debug_end_offset = format.result.col_offset + 1
            debug_metadata = format.metadata
        else:
            debug_end_line = end_lineno
            debug_end_offset = end_col_offset
            debug_metadata = closing_brace.metadata
        debug_text = ast.Constant(debug_metadata, None, lineno, col_offset + 1,
                                  debug_end_line, debug_end_offset - 1)
        return ast.JoinedStr([debug_text, fv], lineno, col_offset,
                             debug_end_line, debug_end_offset)

    def _build_concatenated_bytes(self, strings, lineno, col_offset, end_lineno,
                                  end_col_offset):
        kind = strings[0].kind
        res = b''.join(elem.value for elem in strings)
        return ast.Constant(res, kind, lineno, col_offset, end_lineno, end_col_offset)

    def _build_concatenated_unicode(self, strings, lineno, col_offset, end_lineno,
                                    end_col_offset):
        kind = strings[0].kind
        final = ''.join(elem.value for elem in strings)
        return ast.Constant(final, kind, lineno, col_offset, end_lineno, end_col_offset)

    def _build_concatenated_str(self, strings, lineno, col_offset, end_lineno,
                                end_col_offset):
        flattened = []
        for elem in strings:
            if isinstance(elem, (ast.JoinedStr, ast.TemplateStr)):
                flattened.extend(elem.values)
            else:
                flattened.append(elem)
        values = []
        i = 0
        n = len(flattened)
        while i < n:
            elem = flattened[i]
            if isinstance(elem, ast.Constant):
                if i + 1 < n and isinstance(flattened[i + 1], ast.Constant):
                    first_elem = elem
                    kind = elem.kind
                    parts = []
                    last_elem = elem
                    j = i
                    while j < n:
                        current = flattened[j]
                        if isinstance(current, ast.Constant):
                            parts.append(current.value)
                            last_elem = current
                            j += 1
                        else:
                            break
                    i = j - 1
                    elem = ast.Constant(''.join(parts), kind, first_elem.lineno,
                                        first_elem.col_offset, last_elem.end_lineno,
                                        last_elem.end_col_offset)
                if type(elem.value) is str and len(elem.value) == 0:
                    i += 1
                    continue
            values.append(elem)
            i += 1
        return values

    def concatenate_tstrings(self, strings, lineno, col_offset, end_lineno, end_col_offset):
        values = self._build_concatenated_str(strings, lineno, col_offset, end_lineno,
                                              end_col_offset)
        return ast.TemplateStr(values, lineno, col_offset, end_lineno, end_col_offset)

    def concatenate_strings(self, strings, lineno, col_offset, end_lineno, end_col_offset):
        f_string_found = 0
        unicode_string_found = 0
        bytes_found = 0
        for elem in strings:
            if isinstance(elem, ast.Constant):
                if type(elem.value) is bytes:
                    bytes_found = 1
                else:
                    unicode_string_found = 1
            else:
                f_string_found = 1
        if (unicode_string_found or f_string_found) and bytes_found:
            self.raise_syntax_error("cannot mix bytes and nonbytes literals")
        if not f_string_found:
            if len(strings) == 1:
                return strings[0]
            elif bytes_found:
                return self._build_concatenated_bytes(strings, lineno, col_offset,
                                                      end_lineno, end_col_offset)
            else:
                return self._build_concatenated_unicode(strings, lineno, col_offset,
                                                        end_lineno, end_col_offset)
        values = self._build_concatenated_str(strings, lineno, col_offset, end_lineno,
                                              end_col_offset)
        return ast.JoinedStr(values, lineno, col_offset, end_lineno, end_col_offset)

    def checked_future_import(self, module, names, level, lineno, col_offset,
                              end_lineno, end_col_offset):
        if level == 0 and module == "__future__":
            for alias in names:
                if alias.name == "barry_as_FLUFL":
                    self.flags |= PyPARSE_BARRY_AS_BDFL
        return ast.ImportFrom(module, names, level, lineno, col_offset, end_lineno,
                              end_col_offset)

    def register_stmts(self, stmts):
        if not self.call_invalid_rules:
            return stmts
        if len(stmts) == 0:
            return stmts
        last_stmt = stmts[-1]
        if self.last_stmt_location[0] > last_stmt.lineno:
            return stmts
        self.last_stmt_location = (last_stmt.lineno, last_stmt.col_offset,
                                   last_stmt.end_lineno, last_stmt.end_col_offset)
        return stmts
