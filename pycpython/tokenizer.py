"""Port of CPython 3.14's C tokenizer (Parser/lexer/lexer.c and friends).

The tokenizer works on a UTF-8 *byte* buffer, exactly like the C
implementation, so that column offsets are byte offsets (which is what the
AST ``col_offset`` attributes hold).  Two front ends are provided:

* :class:`TokState` -- the low level state machine used by the parser.
* :func:`generate_tokens` / :func:`tokenize` -- a re-implementation of the
  ``tokenize`` module's public generators (``extra_tokens`` mode), producing
  ``TokenInfo`` tuples with character offsets.

No ``eval``/``exec``/``compile`` is used anywhere in this module.
"""

import collections
import warnings

from . import tokens as T
from .tokens import (ENDMARKER, NAME, NUMBER, STRING, NEWLINE, INDENT, DEDENT,
                     OP, TYPE_IGNORE, TYPE_COMMENT, FSTRING_START,
                     FSTRING_MIDDLE, FSTRING_END, TSTRING_START,
                     TSTRING_MIDDLE, TSTRING_END, COMMENT, NL, ERRORTOKEN,
                     ENCODING, ELLIPSIS, DOT, is_string_lit)

EOF = -1

# Error codes (Include/errcode.h)
E_OK = 10
E_EOF = 11
E_INTR = 12
E_TOKEN = 13
E_SYNTAX = 14
E_NOMEM = 15
E_DONE = 16
E_ERROR = 17
E_TABSPACE = 18
E_OVERFLOW = 19
E_TOODEEP = 20
E_DEDENT = 21
E_DECODE = 22
E_EOFS = 23
E_EOLS = 24
E_LINECONT = 25
E_BADSINGLE = 27
E_INTERACT_STOP = 28
E_COLUMNOVERFLOW = 29

MAXINDENT = 100
MAXLEVEL = 200
MAXFSTRINGLEVEL = 150
MAX_EXPR_NESTING = 3
TABSIZE = 8
ALTTABSIZE = 1

TOK_REGULAR_MODE = 0
TOK_FSTRING_MODE = 1
FSTRING = 0
TSTRING = 1

STATE_INIT = 0
STATE_SEEK_CODING = 1
STATE_NORMAL = 2

IUNDERFLOW_NORMAL = 0
IUNDERFLOW_STOP = 1

_TYPE_COMMENT_PREFIX = b"# type: "


def _isdigit(c):
    return 48 <= c <= 57


def _isxdigit(c):
    return 48 <= c <= 57 or 65 <= c <= 70 or 97 <= c <= 102


def _isalnum(c):
    return 48 <= c <= 57 or 65 <= c <= 90 or 97 <= c <= 122


def _isalpha(c):
    return 65 <= c <= 90 or 97 <= c <= 122


def _tolower(c):
    if 65 <= c <= 90:
        return c + 32
    return c


def _is_potential_identifier_start(c):
    return (97 <= c <= 122) or (65 <= c <= 90) or c == 95 or c >= 128


def _is_potential_identifier_char(c):
    return ((97 <= c <= 122) or (65 <= c <= 90) or (48 <= c <= 57)
            or c == 95 or c >= 128)


def _is_printable(c):
    """Py_UNICODE_ISPRINTABLE for a byte value / code point."""
    if c < 0:
        return True
    try:
        return chr(c).isprintable()
    except ValueError:
        return False


class TokenizerError(Exception):
    """Internal marker; the real exception lives in TokState.exc."""


class TokenizerMode:
    __slots__ = ('kind', 'curly_bracket_depth', 'curly_bracket_expr_start_depth',
                 'quote', 'quote_size', 'raw', 'start', 'multi_line_start',
                 'first_line', 'last_expr_start', 'last_expr_end',
                 'in_debug', 'in_format_spec', 'string_kind')

    def __init__(self):
        self.kind = TOK_REGULAR_MODE
        self.curly_bracket_depth = 0
        self.curly_bracket_expr_start_depth = -1
        self.quote = 0
        self.quote_size = 0
        self.raw = 0
        self.start = None
        self.multi_line_start = None
        self.first_line = 0
        # Instead of the C buffer copy we keep byte indices into the input.
        self.last_expr_start = None
        self.last_expr_end = -1
        self.in_debug = 0
        self.in_format_spec = 0
        self.string_kind = FSTRING


class Token:
    """A raw token produced by :meth:`TokState.get`."""
    __slots__ = ('type', 'start', 'end', 'level', 'lineno', 'col_offset',
                 'end_lineno', 'end_col_offset', 'metadata')

    def __init__(self):
        self.type = ERRORTOKEN
        self.start = None
        self.end = None
        self.level = 0
        self.lineno = 0
        self.col_offset = -1
        self.end_lineno = 0
        self.end_col_offset = -1
        self.metadata = None

    def __repr__(self):
        return "<Token %s %d:%d-%d:%d>" % (T.tok_name.get(self.type, self.type),
                                           self.lineno, self.col_offset,
                                           self.end_lineno, self.end_col_offset)


def translate_newlines(s, exec_input, preserve_crlf):
    """Port of _PyTokenizer_translate_newlines (bytes in, bytes out)."""
    out = bytearray()
    skip_next_lf = False
    c = 0
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if skip_next_lf:
            skip_next_lf = False
            if c == 10:
                i += 1
                if i >= n:
                    c = 0
                    break
                c = s[i]
        if not preserve_crlf and c == 13:
            skip_next_lf = True
            c = 10
        out.append(c)
        i += 1
    if exec_input and c != 10 and c != 0:
        out.append(10)
    return bytes(out)


def _get_normal_name(s):
    buf = s[:12].replace('_', '-').lower()
    if buf == 'utf-8' or buf.startswith('utf-8-'):
        return 'utf-8'
    if (buf in ('latin-1', 'iso-8859-1', 'iso-latin-1')
            or buf.startswith('latin-1-') or buf.startswith('iso-8859-1-')
            or buf.startswith('iso-latin-1-')):
        return 'iso-8859-1'
    return s


def _get_coding_spec(line):
    """Port of get_coding_spec.  Returns encoding name or None."""
    size = len(line)
    i = 0
    while i < size - 6:
        ch = line[i]
        if ch == 35:  # '#'
            break
        if ch not in (32, 9, 12):
            return None
        i += 1
    while i < size - 6:
        if line[i:i + 6] == b'coding':
            t = i + 6
            if t >= size or line[t] not in (58, 61):  # ':' '='
                i += 1
                continue
            t += 1
            while t < size and line[t] in (32, 9):
                t += 1
            begin = t
            while t < size and (_isalnum(line[t]) or line[t] in (45, 95, 46)):
                t += 1
            if begin < t:
                r = line[begin:t].decode('latin-1')
                return _get_normal_name(r)
        i += 1
    return None


def _valid_utf8(s, i):
    """Port of valid_utf8: number of bytes in the sequence at s[i], or 0."""
    c = s[i]
    if c < 0x80:
        return 1
    n = len(s)
    if c < 0xE0:
        if c < 0xC2:
            return 0
        expected = 1
    elif c < 0xF0:
        nxt = s[i + 1] if i + 1 < n else 0
        if c == 0xE0 and nxt < 0xA0:
            return 0
        if c == 0xED and nxt >= 0xA0:
            return 0
        expected = 2
    elif c < 0xF5:
        nxt = s[i + 1] if i + 1 < n else 0
        if (c == 0xF0) if nxt < 0x90 else (c == 0xF4):
            return 0
        expected = 3
    else:
        return 0
    for k in range(1, expected + 1):
        if i + k >= n:
            return 0
        b = s[i + k]
        if b < 0x80 or b >= 0xC0:
            return 0
    return expected + 1


def byte_offset_to_character_offset(line_bytes, col_offset):
    """_PyPegen_byte_offset_to_character_offset.  The C code decodes
    ``col_offset`` bytes of the NUL-terminated UTF-8 line buffer, so an
    offset one past the end of the line includes the terminator (which
    counts as a character)."""
    n = len(line_bytes)
    if col_offset > n + 1:
        col_offset = n + 1
    if col_offset < 0:
        col_offset = 0
    data = bytes(line_bytes) + b'\x00'
    return len(data[:col_offset].decode('utf-8', 'replace'))


def byte_offset_to_character_offset_line(line_bytes, col_offset, end_col_offset):
    """Port of _PyPegen_byte_offset_to_character_offset_line."""
    n = 0
    data = line_bytes
    while col_offset < end_col_offset:
        if col_offset >= len(data):
            break
        ch = data[col_offset]
        if ch < 0x80:
            col_offset += 1
        elif (ch & 0xE0) == 0xC0:
            col_offset += 2
        elif (ch & 0xF0) == 0xE0:
            col_offset += 3
        elif (ch & 0xF8) == 0xF0:
            col_offset += 4
        else:
            raise ValueError("Invalid UTF-8 sequence")
        n += 1
    return n


class TokState:
    """Port of ``struct tok_state`` plus the lexer functions."""

    def __init__(self):
        # The full input buffer (bytes / bytearray).  All "pointers" of the C
        # code are integer indices into ``data``.
        self.data = b''
        self.buf = 0
        self.cur = 0
        self.inp = 0
        self.end = 0
        self.start = None
        self.done = E_OK
        self.tabsize = TABSIZE
        self.indent = 0
        self.indstack = [0] * MAXINDENT
        self.altindstack = [0] * MAXINDENT
        self.atbol = 1
        self.pendin = 0
        self.prompt = None
        self.lineno = 0
        self.first_lineno = 0
        self.starting_col_offset = -1
        self.col_offset = -1
        self.level = 0
        self.parenstack = [0] * MAXLEVEL
        self.parenlinenostack = [0] * MAXLEVEL
        self.parencolstack = [0] * MAXLEVEL
        self.filename = None
        self.decoding_state = STATE_INIT
        self.decoding_erred = 0
        self.encoding = None
        self.cont_line = 0
        self.line_start = 0
        self.multi_line_start = 0
        self.readline = None
        self.enc = None
        self.str = None          # the (translated) source when tokenizing a string
        self.input = None
        self.type_comments = 0
        self.interactive_underflow = IUNDERFLOW_NORMAL
        self.underflow = None
        self.report_warnings = 1
        self.tok_mode_stack = [TokenizerMode()]
        self.tok_mode_stack_index = 0
        self.tok_extra_tokens = 0
        self.comment_newline = 0
        self.implicit_newline = 0
        self.fp_interactive = 0
        self.interactive_src_start = None
        # Pending Python exception (the C code keeps it in the thread state)
        self.exc = None
        self.readline_eof = False

    # ------------------------------------------------------------------
    # Construction helpers (string_tokenizer.c / utf8_tokenizer.c /
    # readline_tokenizer.c)
    # ------------------------------------------------------------------

    @classmethod
    def from_string(cls, src, exec_input, preserve_crlf=0, filename=None):
        """_PyTokenizer_FromString: *src* is bytes; performs BOM/cookie
        detection and decoding."""
        tok = cls()
        tok.filename = filename
        decoded = tok._decode_str(src, exec_input, preserve_crlf)
        if decoded is None:
            raise tok.exc
        tok.data = decoded
        tok.buf = tok.cur = tok.inp = 0
        tok.end = 0
        tok.underflow = tok._underflow_string
        return tok

    @classmethod
    def from_utf8(cls, src, exec_input, preserve_crlf=0):
        """_PyTokenizer_FromUTF8: *src* is bytes already known to be UTF-8."""
        tok = cls()
        translated = translate_newlines(src, exec_input, preserve_crlf)
        tok.input = translated
        tok.decoding_state = STATE_NORMAL
        tok.enc = None
        tok.str = translated
        tok.encoding = 'utf-8'
        tok.data = translated
        tok.buf = tok.cur = tok.inp = 0
        tok.end = 0
        tok.underflow = tok._underflow_string
        return tok

    @classmethod
    def from_readline(cls, readline, encoding, exec_input, preserve_crlf):
        """_PyTokenizer_FromReadline."""
        tok = cls()
        tok.data = bytearray()
        tok.buf = tok.cur = tok.inp = 0
        tok.end = 0
        if encoding is not None:
            tok.encoding = encoding
        tok.decoding_state = STATE_NORMAL
        tok.underflow = tok._underflow_readline
        tok.readline = readline
        return tok

    def _error_ret(self):
        """_PyTokenizer_error_ret."""
        self.decoding_erred = 1
        self.done = E_DECODE
        return None

    def _decode_str(self, inp, single, preserve_crlf):
        s = translate_newlines(inp, single, preserve_crlf)
        self.input = s
        self.enc = None
        self.str = s
        # check_bom
        self.decoding_state = STATE_SEEK_CODING
        if s[:3] == b'\xef\xbb\xbf':
            s = s[3:]
            self.encoding = 'utf-8'
        self.str = s
        self.data = s
        utf8 = None
        # find the first two newlines
        newl = [None, None]
        lineno = 0
        idx = s.find(b'\n')
        if idx >= 0:
            newl[0] = idx
            lineno = 1
            idx2 = s.find(b'\n', idx + 1)
            if idx2 >= 0:
                newl[1] = idx2
                lineno = 2
        self.enc = None
        if newl[0] is not None:
            self.lineno = 1
            if not self._check_coding_spec(s, 0, newl[0]):
                return None
            if (self.enc is None and self.decoding_state != STATE_NORMAL
                    and newl[1] is not None):
                self.lineno = 2
                if not self._check_coding_spec(s, newl[0] + 1, newl[1]):
                    return None
        self.lineno = 0
        if self.enc is not None:
            try:
                text = s.decode(self.enc)
            except (LookupError, UnicodeDecodeError, ValueError) as e:
                self.exc = e
                return self._error_ret()
            utf8 = text.encode('utf-8')
            s = utf8
        elif not self._ensure_utf8(s, 1):
            return self._error_ret()
        if utf8 is not None:
            translated = translate_newlines(s, single, preserve_crlf)
            self.input = translated
            s = translated
        self.str = s
        return s

    def _raise_init_error(self, e):
        """_PyTokenizer_raise_init_error: wrap decoding failures in a
        SyntaxError."""
        if isinstance(e, SyntaxError):
            e.filename = self.filename
            self.exc = e
            return
        if not isinstance(e, (LookupError, SyntaxError, ValueError)):
            self.exc = e
            return
        errstr = str(e)
        exc = SyntaxError(errstr, (self.filename, 0, -1, None))
        self.exc = exc

    def _check_coding_spec(self, s, begin, end):
        """_PyTokenizer_check_coding_spec for the string tokenizer."""
        if self.cont_line:
            self.decoding_state = STATE_NORMAL
            return True
        line = s[begin:end]
        cs = _get_coding_spec(line)
        if cs is None:
            for ch in line:
                if ch in (35, 10, 13):
                    break
                if ch not in (32, 9, 12):
                    self.decoding_state = STATE_NORMAL
                    break
            return True
        self.decoding_state = STATE_NORMAL
        if self.encoding is None:
            if cs != 'utf-8':
                # buf_setreadl: for string input just record the encoding
                self.enc = cs
            self.encoding = cs
        else:
            if self.encoding != cs:
                self.line_start = begin
                self.cur = begin
                self._syntaxerror_known_range(0, len(line),
                                              "encoding problem: %s with BOM" % cs)
                self._error_ret()
                return False
        return True

    def _ensure_utf8(self, line, lineno):
        col_offset = 0
        line_start = 0
        i = 0
        n = len(line)
        badchar = None
        while i < n:
            length = _valid_utf8(line, i)
            if not length:
                badchar = i
                break
            col_offset += 1
            if line[i] == 10:
                lineno += 1
                col_offset = 0
                line_start = i + 1
            i += length
        if badchar is not None:
            self.lineno = lineno
            self.line_start = line_start
            self.cur = badchar
            self.data = line
            self._syntaxerror_known_range(
                col_offset + 1, col_offset + 1,
                "Non-UTF-8 code starting with '\\x%.2x'%s%s on line %i, "
                "but no encoding declared; "
                "see https://peps.python.org/pep-0263/ for details"
                % (line[badchar], " in file " if self.filename else "",
                   self.filename if self.filename else "", lineno))
            return False
        return True

    # ------------------------------------------------------------------
    # Underflow (line reading)
    # ------------------------------------------------------------------

    def _underflow_string(self):
        data = self.data
        end = data.find(b'\n', self.inp)
        if end >= 0:
            end += 1
        else:
            end = len(data)
            if end == self.inp:
                self.done = E_EOF
                return 0
        if self.start is None:
            self.buf = self.cur
        self.line_start = self.cur
        self.lineno += 1
        self.col_offset = 0
        self.inp = end
        return 1

    def _underflow_readline(self):
        if self.start is None and not self.inside_fstring():
            # C discards the buffer here; we keep the bytes but move buf.
            self.cur = self.inp = len(self.data)
            self.buf = self.cur
        if not self._readline_string():
            return 0
        if self.inp == self.cur:
            self.done = E_EOF
            return 0
        self.implicit_newline = 0
        if self.data[self.inp - 1] != 10:
            self.data.append(10)
            self.inp += 1
            self.implicit_newline = 1
        # _PyLexer_update_ftstring_expr(tok, 0) is a no-op with index tracking
        self.lineno += 1
        self.col_offset = 0
        if self.encoding is None and not self._ensure_utf8_buffer(self.cur, self.lineno):
            self._error_ret()
            return 0
        return self.done == E_OK

    def _ensure_utf8_buffer(self, start, lineno):
        line = bytes(self.data[start:])
        col_offset = 0
        line_start = start
        i = 0
        n = len(line)
        while i < n:
            length = _valid_utf8(line, i)
            if not length:
                self.lineno = lineno
                self.line_start = line_start
                self.cur = start + i
                self._syntaxerror_known_range(
                    col_offset + 1, col_offset + 1,
                    "Non-UTF-8 code starting with '\\x%.2x'%s%s on line %i, "
                    "but no encoding declared; "
                    "see https://peps.python.org/pep-0263/ for details"
                    % (line[i], " in file " if self.filename else "",
                       self.filename if self.filename else "", lineno))
                return False
            col_offset += 1
            if line[i] == 10:
                lineno += 1
                col_offset = 0
                line_start = start + i + 1
            i += length
        return True

    def _readline_string(self):
        if self.readline_eof:
            return 1
        try:
            raw_line = self.readline()
        except StopIteration:
            self.readline_eof = True
            return 1
        if self.encoding is not None:
            if not isinstance(raw_line, bytes):
                self.exc = TypeError("readline() returned a non-bytes object")
                self._error_ret()
                return 0
            line = raw_line.decode(self.encoding, 'replace')
        else:
            if not isinstance(raw_line, str):
                self.exc = TypeError("readline() returned a non-string object")
                self._error_ret()
                return 0
            line = raw_line
        buf = line.encode('utf-8', 'surrogatepass')
        if b'\x00' in buf:
            pass
        self.data.extend(buf)
        self.inp += len(buf)
        self.line_start = self.cur
        return 1

    # ------------------------------------------------------------------
    # Character access
    # ------------------------------------------------------------------

    def nextc(self):
        while True:
            if self.cur != self.inp:
                self.col_offset += 1
                c = self.data[self.cur]
                self.cur += 1
                return c
            if self.done != E_OK:
                return EOF
            rc = self.underflow()
            if not rc:
                self.cur = self.inp
                return EOF
            self.line_start = self.cur
            if self.data.find(b'\x00', self.line_start, self.inp) >= 0:
                self._syntaxerror("source code cannot contain null bytes")
                self.cur = self.inp
                return EOF

    def backup(self, c):
        if c != EOF:
            self.cur -= 1
            if self.cur < 0:
                raise SystemError("tokenizer beginning of buffer")
            if self.data[self.cur] != c:
                raise SystemError("tok_backup: wrong character")
            self.col_offset -= 1

    def mode(self):
        return self.tok_mode_stack[self.tok_mode_stack_index]

    def next_mode(self):
        self.tok_mode_stack_index += 1
        if self.tok_mode_stack_index >= len(self.tok_mode_stack):
            self.tok_mode_stack.append(TokenizerMode())
        m = TokenizerMode()
        self.tok_mode_stack[self.tok_mode_stack_index] = m
        return m

    def inside_fstring(self):
        return self.tok_mode_stack_index > 0

    def string_prefix(self):
        return 't' if self.mode().string_kind == TSTRING else 'f'

    # ------------------------------------------------------------------
    # Errors and warnings (Parser/tokenizer/helpers.c)
    # ------------------------------------------------------------------

    def _line_text(self):
        """Current line, decoded with 'replace', up to the cursor / newline."""
        ls = self.line_start
        errtext_cur = bytes(self.data[ls:self.cur]).decode('utf-8', 'replace')
        nl = self.data.find(b'\n', ls)
        if nl < 0:
            nl = len(self.data)
        line_len = nl - ls
        if line_len != self.cur - ls:
            return bytes(self.data[ls:nl]).decode('utf-8', 'replace'), errtext_cur
        return errtext_cur, errtext_cur

    def _syntaxerror_range(self, msg, col_offset, end_col_offset):
        if self.done == E_ERROR:
            return ERRORTOKEN
        errtext, errtext_cur = self._line_text()
        if col_offset == -1:
            col_offset = len(errtext_cur)
        if end_col_offset == -1:
            end_col_offset = col_offset
        self.exc = SyntaxError(msg, (self.filename, self.lineno, col_offset,
                                     errtext, self.lineno, end_col_offset))
        self.done = E_ERROR
        return ERRORTOKEN

    def _syntaxerror(self, msg):
        return self._syntaxerror_range(msg, -1, -1)

    def _syntaxerror_known_range(self, col_offset, end_col_offset, msg):
        return self._syntaxerror_range(msg, col_offset, end_col_offset)

    def _indenterror(self):
        self.done = E_TABSPACE
        self.cur = self.inp
        return ERRORTOKEN

    def _warn(self, category, msg):
        """_PyTokenizer_parser_warn."""
        if not self.report_warnings:
            return 0
        try:
            warnings.warn_explicit(msg, category, self.filename, self.lineno)
        except category:
            self._syntaxerror(msg)
            self.done = E_ERROR
            return -1
        except Exception as e:  # pragma: no cover
            self.exc = e
            self.done = E_ERROR
            return -1
        return 0

    def _warn_invalid_escape_sequence(self, first_invalid_escape_char):
        if not self.report_warnings:
            return 0
        ch = chr(first_invalid_escape_char)
        msg = ('"\\%s" is an invalid escape sequence. '
               'Such sequences will not work in the future. '
               'Did you mean "\\\\%s"? A raw string is also an option.' % (ch, ch))
        try:
            warnings.warn_explicit(msg, SyntaxWarning, self.filename, self.lineno)
        except SyntaxWarning:
            return self._syntaxerror(
                '"\\%s" is an invalid escape sequence. '
                'Did you mean "\\\\%s"? A raw string is also an option.' % (ch, ch))
        except Exception as e:  # pragma: no cover
            self.exc = e
            return -1
        return 0

    # ------------------------------------------------------------------
    # f-string expression tracking (metadata for debug '=' / t-strings)
    # ------------------------------------------------------------------

    def _set_ftstring_expr(self, token, c):
        tok_mode = self.mode()
        if not (tok_mode.in_debug or tok_mode.string_kind == TSTRING) or token.metadata is not None:
            return 0
        if tok_mode.last_expr_start is None:
            return 0
        endidx = tok_mode.last_expr_end if tok_mode.last_expr_end >= 0 else self.start
        buf = bytes(self.data[tok_mode.last_expr_start:endidx])
        # Look for a '#' character outside of string literals
        hash_detected = False
        in_string = False
        quote_char = 0
        i = 0
        n = len(buf)
        while i < n:
            ch = buf[i]
            if ch == 92:  # backslash
                i += 2
                continue
            if ch == 34 or ch == 39:
                if not in_string:
                    in_string = True
                    quote_char = ch
                elif ch == quote_char:
                    in_string = False
                i += 1
                continue
            if ch == 35 and not in_string:
                hash_detected = True
                break
            i += 1
        if hash_detected:
            result = bytearray()
            in_string = False
            quote_char = 0
            i = 0
            while i < n:
                ch = buf[i]
                if ch == 34 or ch == 39:
                    if not in_string:
                        in_string = True
                        quote_char = ch
                    elif ch == quote_char:
                        in_string = False
                    result.append(ch)
                elif ch == 35 and not in_string:
                    while i < n and buf[i] != 10:
                        i += 1
                    if i < n:
                        result.append(10)
                else:
                    result.append(ch)
                i += 1
            try:
                res = bytes(result).decode('utf-8')
            except UnicodeDecodeError as e:
                self.exc = e
                return -1
        else:
            try:
                res = buf.decode('utf-8')
            except UnicodeDecodeError as e:
                self.exc = e
                return -1
        token.metadata = res
        return 0

    def _update_ftstring_expr(self, cur):
        tok_mode = self.mode()
        if cur == 0:
            return 1
        elif cur == 123:  # '{'
            tok_mode.last_expr_start = self.cur
            tok_mode.last_expr_end = -1
        elif cur == 125 or cur == 33:  # '}' '!'
            tok_mode.last_expr_end = self.start
        elif cur == 58:  # ':'
            if tok_mode.last_expr_end == -1:
                tok_mode.last_expr_end = self.start
        return 1

    # ------------------------------------------------------------------
    # Token construction
    # ------------------------------------------------------------------

    def _make_token(self, token, type_, p_start, p_end):
        token.level = self.level
        if is_string_lit(type_):
            token.lineno = self.first_lineno
        else:
            token.lineno = self.lineno
        token.end_lineno = self.lineno
        token.col_offset = token.end_col_offset = -1
        token.start = p_start
        token.end = p_end
        if p_start is not None and p_end is not None:
            token.col_offset = self.starting_col_offset
            token.end_col_offset = self.col_offset
        token.type = type_
        return type_

    def _make_type_comment_token(self, token, type_, col_offset, end_col_offset,
                                 p_start, p_end):
        token.level = self.level
        token.lineno = token.end_lineno = self.lineno
        token.col_offset = col_offset
        token.end_col_offset = end_col_offset
        token.start = p_start
        token.end = p_end
        token.type = type_
        return type_

    # ------------------------------------------------------------------
    # Lexer helpers
    # ------------------------------------------------------------------

    def _lookahead(self, test):
        """lookahead(): check that the next chars are *test* (bytes) and are
        not followed by an identifier char."""
        consumed = []
        res = 0
        i = 0
        while True:
            c = self.nextc()
            if i == len(test):
                res = not _is_potential_identifier_char(c)
            elif c == test[i]:
                consumed.append(c)
                i += 1
                continue
            self.backup(c)
            while consumed:
                self.backup(consumed.pop())
            return res

    def _verify_end_of_number(self, c, kind):
        if self.tok_extra_tokens:
            return 1
        r = 0
        if c == 97:      # 'a'
            r = self._lookahead(b"nd")
        elif c == 101:   # 'e'
            r = self._lookahead(b"lse")
        elif c == 102:   # 'f'
            r = self._lookahead(b"or")
        elif c == 105:   # 'i'
            c2 = self.nextc()
            if c2 in (102, 110, 115):
                r = 1
            self.backup(c2)
        elif c == 111:   # 'o'
            r = self._lookahead(b"r")
        elif c == 110:   # 'n'
            r = self._lookahead(b"ot")
        if r:
            self.backup(c)
            if self._warn(SyntaxWarning, "invalid %s literal" % kind):
                return 0
            self.nextc()
        elif c < 128 and _is_potential_identifier_char(c):
            self.backup(c)
            self._syntaxerror("invalid %s literal" % kind)
            return 0
        return 1

    def _verify_identifier(self):
        if self.tok_extra_tokens:
            return 1
        if self.decoding_erred:
            return 0
        raw = bytes(self.data[self.start:self.cur])
        try:
            s = raw.decode('utf-8')
        except UnicodeDecodeError as e:
            self.exc = e
            self.done = E_DECODE
            return 0
        # _PyUnicode_ScanIdentifier: length of the longest valid prefix
        invalid = 0
        n = len(s)
        if n and (s[0] == '_' or s[0].isidentifier()):
            invalid = 1
            while invalid < n and ('a' + s[invalid]).isidentifier():
                invalid += 1
        if invalid < n:
            ch = s[invalid]
            if invalid + 1 < n:
                self.cur = self.start + len(s[:invalid + 1].encode('utf-8'))
            if ch.isprintable():
                self._syntaxerror("invalid character '%s' (U+%04X)" % (ch, ord(ch)))
            else:
                self._syntaxerror("invalid non-printable character U+%04X" % ord(ch))
            return 0
        return 1

    def _decimal_tail(self):
        while True:
            c = self.nextc()
            while _isdigit(c):
                c = self.nextc()
            if c != 95:
                break
            c = self.nextc()
            if not _isdigit(c):
                self.backup(c)
                self._syntaxerror("invalid decimal literal")
                return 0
        return c

    def _continuation_line(self):
        c = self.nextc()
        if c == 13:
            c = self.nextc()
        if c != 10:
            self.done = E_LINECONT
            return -1
        c = self.nextc()
        if c == EOF:
            self.done = E_EOF
            self.cur = self.inp
            return -1
        else:
            self.backup(c)
        return c

    def _maybe_raise_syntax_error_for_string_prefixes(self, saw_b, saw_r, saw_u,
                                                      saw_f, saw_t):
        def err(p1, p2):
            self._syntaxerror_known_range(
                self.start + 1 - self.line_start, self.cur - self.line_start,
                "'%s' and '%s' prefixes are incompatible" % (p1, p2))
            return -1
        if saw_u and saw_b:
            return err('u', 'b')
        if saw_u and saw_r:
            return err('u', 'r')
        if saw_u and saw_f:
            return err('u', 'f')
        if saw_u and saw_t:
            return err('u', 't')
        if saw_b and saw_f:
            return err('b', 'f')
        if saw_b and saw_t:
            return err('b', 't')
        if saw_f and saw_t:
            return err('f', 't')
        return 0

    # ------------------------------------------------------------------
    # The main lexer: normal mode
    # ------------------------------------------------------------------

    def _get_normal_mode(self, current_tok, token):
        MK = self._make_token
        while True:  # nextline:
            self.start = None
            self.starting_col_offset = -1
            blankline = 0

            # Get indentation level
            if self.atbol:
                col = 0
                altcol = 0
                self.atbol = 0
                cont_line_col = 0
                while True:
                    c = self.nextc()
                    if c == 32:
                        col += 1
                        altcol += 1
                    elif c == 9:
                        col = (col // self.tabsize + 1) * self.tabsize
                        altcol = (altcol // ALTTABSIZE + 1) * ALTTABSIZE
                    elif c == 12:
                        col = altcol = 0
                    elif c == 92:
                        cont_line_col = cont_line_col if cont_line_col else col
                        c = self._continuation_line()
                        if c == -1:
                            return MK(token, ERRORTOKEN, None, None)
                    elif c == EOF and self.exc is not None:
                        return MK(token, ERRORTOKEN, None, None)
                    else:
                        break
                self.backup(c)
                if c == 35 or c == 10 or c == 13:
                    if col == 0 and c == 10 and self.prompt is not None:
                        blankline = 0
                    elif self.prompt is not None and self.lineno == 1:
                        blankline = 0
                        col = altcol = 0
                    else:
                        blankline = 1
                if not blankline and self.level == 0:
                    col = cont_line_col if cont_line_col else col
                    altcol = cont_line_col if cont_line_col else altcol
                    if col == self.indstack[self.indent]:
                        if altcol != self.altindstack[self.indent]:
                            return MK(token, self._indenterror(), None, None)
                    elif col > self.indstack[self.indent]:
                        if self.indent + 1 >= MAXINDENT:
                            self.done = E_TOODEEP
                            self.cur = self.inp
                            return MK(token, ERRORTOKEN, None, None)
                        if altcol <= self.altindstack[self.indent]:
                            return MK(token, self._indenterror(), None, None)
                        self.pendin += 1
                        self.indent += 1
                        self.indstack[self.indent] = col
                        self.altindstack[self.indent] = altcol
                    else:
                        while self.indent > 0 and col < self.indstack[self.indent]:
                            self.pendin -= 1
                            self.indent -= 1
                        if col != self.indstack[self.indent]:
                            self.done = E_DEDENT
                            self.cur = self.inp
                            return MK(token, ERRORTOKEN, None, None)
                        if altcol != self.altindstack[self.indent]:
                            return MK(token, self._indenterror(), None, None)

            self.start = self.cur
            self.starting_col_offset = self.col_offset

            # Return pending indents/dedents
            if self.pendin != 0:
                if self.pendin < 0:
                    p_start = p_end = None
                    if self.tok_extra_tokens:
                        p_start = self.cur
                        p_end = self.cur
                    self.pendin += 1
                    return MK(token, DEDENT, p_start, p_end)
                else:
                    p_start = p_end = None
                    if self.tok_extra_tokens:
                        p_start = self.buf
                        p_end = self.cur
                    self.pendin -= 1
                    return MK(token, INDENT, p_start, p_end)

            # Peek ahead at the next character
            c = self.nextc()
            self.backup(c)

            while True:  # again:
                self.start = None
                # Skip spaces
                while True:
                    c = self.nextc()
                    if not (c == 32 or c == 9 or c == 12):
                        break
                # Set start of current token
                self.start = self.cur - 1
                self.starting_col_offset = self.col_offset - 1

                # Skip comment, unless it's a type comment
                if c == 35:
                    p = None
                    while c != EOF and c != 10 and c != 13:
                        c = self.nextc()
                    if self.tok_extra_tokens:
                        p = self.start
                    if self.type_comments:
                        p = self.start
                        current_starting_col_offset = self.starting_col_offset
                        prefix = _TYPE_COMMENT_PREFIX
                        pi = 0
                        while pi < len(prefix) and p < self.cur:
                            if prefix[pi] == 32:
                                while self.data[p] in (32, 9):
                                    p += 1
                                    current_starting_col_offset += 1
                            elif prefix[pi] == self.data[p]:
                                p += 1
                                current_starting_col_offset += 1
                            else:
                                break
                            pi += 1
                        if pi == len(prefix):
                            ignore_end = p + 6
                            ignore_end_col_offset = current_starting_col_offset + 6
                            self.backup(c)
                            type_start = p
                            is_type_ignore = (
                                self.cur >= ignore_end
                                and bytes(self.data[p:p + 6]) == b"ignore"
                                and not (self.cur > ignore_end
                                         and (self.data[ignore_end] >= 128
                                              or _isalnum(self.data[ignore_end]))))
                            if is_type_ignore:
                                p_start = ignore_end
                                p_end = self.cur
                                if blankline:
                                    self.nextc()
                                    self.atbol = 1
                                return self._make_type_comment_token(
                                    token, TYPE_IGNORE, ignore_end_col_offset,
                                    self.col_offset, p_start, p_end)
                            else:
                                p_start = type_start
                                p_end = self.cur
                                return self._make_type_comment_token(
                                    token, TYPE_COMMENT, current_starting_col_offset,
                                    self.col_offset, p_start, p_end)
                    if self.tok_extra_tokens:
                        self.backup(c)
                        p_start = p
                        p_end = self.cur
                        self.comment_newline = blankline
                        return MK(token, COMMENT, p_start, p_end)

                if self.done == E_INTERACT_STOP:
                    return MK(token, ENDMARKER, None, None)

                # Check for EOF and errors now
                if c == EOF:
                    if self.level:
                        return MK(token, ERRORTOKEN, None, None)
                    return MK(token, ENDMARKER if self.done == E_EOF else ERRORTOKEN,
                              None, None)

                # Identifier (most frequent token!)
                nonascii = 0
                if _is_potential_identifier_start(c):
                    saw_b = saw_r = saw_u = saw_f = saw_t = 0
                    goto_quote = None
                    while True:
                        if not saw_b and (c == 98 or c == 66):
                            saw_b = 1
                        elif not saw_u and (c == 117 or c == 85):
                            saw_u = 1
                        elif not saw_r and (c == 114 or c == 82):
                            saw_r = 1
                        elif not saw_f and (c == 102 or c == 70):
                            saw_f = 1
                        elif not saw_t and (c == 116 or c == 84):
                            saw_t = 1
                        else:
                            break
                        c = self.nextc()
                        if c == 34 or c == 39:
                            status = self._maybe_raise_syntax_error_for_string_prefixes(
                                saw_b, saw_r, saw_u, saw_f, saw_t)
                            if status < 0:
                                return MK(token, ERRORTOKEN, None, None)
                            if saw_f or saw_t:
                                goto_quote = 'f_string_quote'
                            else:
                                goto_quote = 'letter_quote'
                            break
                    if goto_quote is None:
                        while _is_potential_identifier_char(c):
                            if c >= 128:
                                nonascii = 1
                            c = self.nextc()
                        self.backup(c)
                        if nonascii and not self._verify_identifier():
                            return MK(token, ERRORTOKEN, None, None)
                        return MK(token, NAME, self.start, self.cur)
                    if goto_quote == 'f_string_quote':
                        return self._f_string_quote(c, token)
                    return self._letter_quote(c, token)

                if c == 13:
                    c = self.nextc()

                # Newline
                if c == 10:
                    self.atbol = 1
                    if blankline or self.level > 0:
                        if self.tok_extra_tokens:
                            if self.comment_newline:
                                self.comment_newline = 0
                            return MK(token, NL, self.start, self.cur)
                        break  # goto nextline
                    if self.comment_newline and self.tok_extra_tokens:
                        self.comment_newline = 0
                        return MK(token, NL, self.start, self.cur)
                    self.cont_line = 0
                    return MK(token, NEWLINE, self.start, self.cur - 1)

                # Period or number starting with period?
                if c == 46:
                    c = self.nextc()
                    if _isdigit(c):
                        return self._number_fraction(c, token)
                    elif c == 46:
                        c = self.nextc()
                        if c == 46:
                            return MK(token, ELLIPSIS, self.start, self.cur)
                        else:
                            self.backup(c)
                        self.backup(46)
                    else:
                        self.backup(c)
                    return MK(token, DOT, self.start, self.cur)

                # Number
                if _isdigit(c):
                    return self._number(c, token)

                if c == 39 or c == 34:
                    return self._letter_quote(c, token)

                # Line continuation
                if c == 92:
                    c = self._continuation_line()
                    if c == -1:
                        return MK(token, ERRORTOKEN, None, None)
                    self.cont_line = 1
                    continue  # goto again

                # Punctuation character
                is_punctuation = c in (58, 125, 33, 123)
                if (is_punctuation and self.inside_fstring()
                        and current_tok.curly_bracket_expr_start_depth >= 0):
                    cursor = current_tok.curly_bracket_depth - (1 if c != 123 else 0)
                    in_format_spec = current_tok.in_format_spec
                    cursor_in_format_with_debug = (
                        cursor == 1 and (current_tok.in_debug or in_format_spec))
                    cursor_valid = cursor == 0 or cursor_in_format_with_debug
                    if cursor_valid and not self._update_ftstring_expr(c):
                        return MK(token, ENDMARKER, None, None)
                    if cursor_valid and c != 123 and self._set_ftstring_expr(token, c):
                        return MK(token, ERRORTOKEN, None, None)
                    if c == 58 and cursor == current_tok.curly_bracket_expr_start_depth:
                        current_tok.kind = TOK_FSTRING_MODE
                        current_tok.in_format_spec = 1
                        return MK(token, T.ONE_CHAR.get(c, OP), self.start, self.cur)

                # Check for two-character token
                c2 = self.nextc()
                current_token = T.TWO_CHARS.get((c, c2), OP)
                if current_token != OP:
                    c3 = self.nextc()
                    current_token3 = T.THREE_CHARS.get((c, c2, c3), OP)
                    if current_token3 != OP:
                        current_token = current_token3
                    else:
                        self.backup(c3)
                    return MK(token, current_token, self.start, self.cur)
                self.backup(c2)

                # Keep track of parentheses nesting level
                if c in (40, 91, 123):
                    if self.level >= MAXLEVEL:
                        return MK(token, self._syntaxerror("too many nested parentheses"),
                                  None, None)
                    self.parenstack[self.level] = c
                    self.parenlinenostack[self.level] = self.lineno
                    self.parencolstack[self.level] = self.start - self.line_start
                    self.level += 1
                    if self.inside_fstring():
                        current_tok.curly_bracket_depth += 1
                elif c in (41, 93, 125):
                    if self.inside_fstring() and not current_tok.curly_bracket_depth and c == 125:
                        return MK(token, self._syntaxerror(
                            "%s-string: single '}' is not allowed" % self.string_prefix()),
                            None, None)
                    if not self.tok_extra_tokens and not self.level:
                        return MK(token, self._syntaxerror("unmatched '%s'" % chr(c)),
                                  None, None)
                    if self.level > 0:
                        self.level -= 1
                        opening = self.parenstack[self.level]
                        if not self.tok_extra_tokens and not (
                                (opening == 40 and c == 41) or (opening == 91 and c == 93)
                                or (opening == 123 and c == 125)):
                            if self.inside_fstring() and opening == 123:
                                previous_bracket = current_tok.curly_bracket_depth - 1
                                if previous_bracket == current_tok.curly_bracket_expr_start_depth:
                                    return MK(token, self._syntaxerror(
                                        "%s-string: unmatched '%s'" % (self.string_prefix(), chr(c))),
                                        None, None)
                            if self.parenlinenostack[self.level] != self.lineno:
                                return MK(token, self._syntaxerror(
                                    "closing parenthesis '%s' does not match "
                                    "opening parenthesis '%s' on line %d"
                                    % (chr(c), chr(opening), self.parenlinenostack[self.level])),
                                    None, None)
                            else:
                                return MK(token, self._syntaxerror(
                                    "closing parenthesis '%s' does not match "
                                    "opening parenthesis '%s'" % (chr(c), chr(opening))),
                                    None, None)
                    if self.inside_fstring():
                        current_tok.curly_bracket_depth -= 1
                        if current_tok.curly_bracket_depth < 0:
                            return MK(token, self._syntaxerror(
                                "%s-string: unmatched '%s'" % (self.string_prefix(), chr(c))),
                                None, None)
                        if (c == 125 and current_tok.curly_bracket_depth
                                == current_tok.curly_bracket_expr_start_depth):
                            current_tok.curly_bracket_expr_start_depth -= 1
                            current_tok.kind = TOK_FSTRING_MODE
                            current_tok.in_format_spec = 0
                            current_tok.in_debug = 0

                if not _is_printable(c):
                    return MK(token, self._syntaxerror(
                        "invalid non-printable character U+%04X" % c), None, None)

                if c == 61 and (current_tok.curly_bracket_depth
                                - current_tok.curly_bracket_expr_start_depth == 1):
                    current_tok.in_debug = 1

                # Punctuation character
                return MK(token, T.ONE_CHAR.get(c, OP), self.start, self.cur)
            # end of 'again' loop -> nextline
            continue

    # -- number scanning (split out of the big function; C uses gotos) ------

    def _number(self, c, token):
        MK = self._make_token
        if c == 48:
            c = self.nextc()
            if c == 120 or c == 88:
                c = self.nextc()
                while True:
                    if c == 95:
                        c = self.nextc()
                    if not _isxdigit(c):
                        self.backup(c)
                        return MK(token, self._syntaxerror("invalid hexadecimal literal"),
                                  None, None)
                    while True:
                        c = self.nextc()
                        if not _isxdigit(c):
                            break
                    if c != 95:
                        break
                if not self._verify_end_of_number(c, "hexadecimal"):
                    return MK(token, ERRORTOKEN, None, None)
            elif c == 111 or c == 79:
                c = self.nextc()
                while True:
                    if c == 95:
                        c = self.nextc()
                    if c < 48 or c >= 56:
                        if _isdigit(c):
                            return MK(token, self._syntaxerror(
                                "invalid digit '%s' in octal literal" % chr(c)), None, None)
                        else:
                            self.backup(c)
                            return MK(token, self._syntaxerror("invalid octal literal"),
                                      None, None)
                    while True:
                        c = self.nextc()
                        if not (48 <= c < 56):
                            break
                    if c != 95:
                        break
                if _isdigit(c):
                    return MK(token, self._syntaxerror(
                        "invalid digit '%s' in octal literal" % chr(c)), None, None)
                if not self._verify_end_of_number(c, "octal"):
                    return MK(token, ERRORTOKEN, None, None)
            elif c == 98 or c == 66:
                c = self.nextc()
                while True:
                    if c == 95:
                        c = self.nextc()
                    if c != 48 and c != 49:
                        if _isdigit(c):
                            return MK(token, self._syntaxerror(
                                "invalid digit '%s' in binary literal" % chr(c)), None, None)
                        else:
                            self.backup(c)
                            return MK(token, self._syntaxerror("invalid binary literal"),
                                      None, None)
                    while True:
                        c = self.nextc()
                        if not (c == 48 or c == 49):
                            break
                    if c != 95:
                        break
                if _isdigit(c):
                    return MK(token, self._syntaxerror(
                        "invalid digit '%s' in binary literal" % chr(c)), None, None)
                if not self._verify_end_of_number(c, "binary"):
                    return MK(token, ERRORTOKEN, None, None)
            else:
                nonzero = 0
                while True:
                    if c == 95:
                        c = self.nextc()
                        if not _isdigit(c):
                            self.backup(c)
                            return MK(token, self._syntaxerror("invalid decimal literal"),
                                      None, None)
                    if c != 48:
                        break
                    c = self.nextc()
                zeros_end = self.cur
                if _isdigit(c):
                    nonzero = 1
                    c = self._decimal_tail()
                    if c == 0:
                        return MK(token, ERRORTOKEN, None, None)
                if c == 46:
                    c = self.nextc()
                    return self._number_fraction(c, token)
                elif c == 101 or c == 69:
                    return self._number_exponent(c, token)
                elif c == 106 or c == 74:
                    return self._number_imaginary(token)
                elif nonzero and not self.tok_extra_tokens:
                    self.backup(c)
                    return MK(token, self._syntaxerror_known_range(
                        self.start + 1 - self.line_start,
                        zeros_end - self.line_start,
                        "leading zeros in decimal integer literals are not permitted; "
                        "use an 0o prefix for octal integers"), None, None)
                if not self._verify_end_of_number(c, "decimal"):
                    return MK(token, ERRORTOKEN, None, None)
        else:
            c = self._decimal_tail()
            if c == 0:
                return MK(token, ERRORTOKEN, None, None)
            if c == 46:
                c = self.nextc()
                return self._number_fraction(c, token)
            return self._number_after_fraction(c, token)
        self.backup(c)
        return MK(token, NUMBER, self.start, self.cur)

    def _number_fraction(self, c, token):
        MK = self._make_token
        if _isdigit(c):
            c = self._decimal_tail()
            if c == 0:
                return MK(token, ERRORTOKEN, None, None)
        return self._number_after_fraction(c, token)

    def _number_after_fraction(self, c, token):
        MK = self._make_token
        if c == 101 or c == 69:
            return self._number_exponent(c, token)
        if c == 106 or c == 74:
            return self._number_imaginary(token)
        elif not self._verify_end_of_number(c, "decimal"):
            return MK(token, ERRORTOKEN, None, None)
        self.backup(c)
        return MK(token, NUMBER, self.start, self.cur)

    def _number_exponent(self, c, token):
        MK = self._make_token
        e = c
        c = self.nextc()
        if c == 43 or c == 45:
            c = self.nextc()
            if not _isdigit(c):
                self.backup(c)
                return MK(token, self._syntaxerror("invalid decimal literal"), None, None)
        elif not _isdigit(c):
            self.backup(c)
            if not self._verify_end_of_number(e, "decimal"):
                return MK(token, ERRORTOKEN, None, None)
            self.backup(e)
            return MK(token, NUMBER, self.start, self.cur)
        c = self._decimal_tail()
        if c == 0:
            return MK(token, ERRORTOKEN, None, None)
        if c == 106 or c == 74:
            return self._number_imaginary(token)
        elif not self._verify_end_of_number(c, "decimal"):
            return MK(token, ERRORTOKEN, None, None)
        self.backup(c)
        return MK(token, NUMBER, self.start, self.cur)

    def _number_imaginary(self, token):
        MK = self._make_token
        c = self.nextc()
        if not self._verify_end_of_number(c, "imaginary"):
            return MK(token, ERRORTOKEN, None, None)
        self.backup(c)
        return MK(token, NUMBER, self.start, self.cur)

    # -- string scanning -----------------------------------------------------

    def _f_string_quote(self, c, token):
        MK = self._make_token
        first = _tolower(self.data[self.start])
        if not ((first in (102, 114, 116)) and (c == 39 or c == 34)):
            return self._letter_quote(c, token)
        quote = c
        quote_size = 1
        self.first_lineno = self.lineno
        self.multi_line_start = self.line_start
        after_quote = self.nextc()
        if after_quote == quote:
            after_after_quote = self.nextc()
            if after_after_quote == quote:
                quote_size = 3
            else:
                self.backup(after_after_quote)
                self.backup(after_quote)
        if after_quote != quote:
            self.backup(after_quote)
        p_start = self.start
        p_end = self.cur
        if self.tok_mode_stack_index + 1 >= MAXFSTRINGLEVEL:
            return MK(token, self._syntaxerror("too many nested f-strings or t-strings"),
                      None, None)
        m = self.next_mode()
        m.kind = TOK_FSTRING_MODE
        m.quote = quote
        m.quote_size = quote_size
        m.start = self.start
        m.multi_line_start = self.line_start
        m.first_line = self.lineno
        m.last_expr_start = None
        m.last_expr_end = -1
        m.in_format_spec = 0
        m.in_debug = 0
        string_kind = FSTRING
        second = _tolower(self.data[self.start + 1])
        if first == 116:
            m.raw = 1 if second == 114 else 0
            string_kind = TSTRING
        elif first == 102:
            m.raw = 1 if second == 114 else 0
        elif first == 114:
            m.raw = 1
            if second == 116:
                string_kind = TSTRING
        m.string_kind = string_kind
        m.curly_bracket_depth = 0
        m.curly_bracket_expr_start_depth = -1
        return MK(token, TSTRING_START if string_kind == TSTRING else FSTRING_START,
                  p_start, p_end)

    def _letter_quote(self, c, token):
        MK = self._make_token
        quote = c
        quote_size = 1
        end_quote_size = 0
        has_escaped_quote = 0
        self.first_lineno = self.lineno
        self.multi_line_start = self.line_start
        c = self.nextc()
        if c == quote:
            c = self.nextc()
            if c == quote:
                quote_size = 3
            else:
                end_quote_size = 1
        if c != quote:
            self.backup(c)
        while end_quote_size != quote_size:
            c = self.nextc()
            if self.done == E_ERROR:
                return MK(token, ERRORTOKEN, None, None)
            if self.done == E_DECODE:
                break
            if c == EOF or (quote_size == 1 and c == 10):
                self.cur = self.start
                self.cur += 1
                self.line_start = self.multi_line_start
                start = self.lineno
                self.lineno = self.first_lineno
                if self.inside_fstring():
                    m = self.mode()
                    if m.quote == quote and m.quote_size == quote_size:
                        return MK(token, self._syntaxerror(
                            "%s-string: expecting '}'" % self.string_prefix()), None, None)
                if quote_size == 3:
                    self._syntaxerror("unterminated triple-quoted string literal"
                                      " (detected at line %d)" % start)
                    if c != 10:
                        self.done = E_EOFS
                    return MK(token, ERRORTOKEN, None, None)
                else:
                    if has_escaped_quote:
                        self._syntaxerror(
                            "unterminated string literal (detected at line %d); "
                            "perhaps you escaped the end quote?" % start)
                    else:
                        self._syntaxerror(
                            "unterminated string literal (detected at line %d)" % start)
                    if c != 10:
                        self.done = E_EOLS
                    return MK(token, ERRORTOKEN, None, None)
            if c == quote:
                end_quote_size += 1
            else:
                end_quote_size = 0
                if c == 92:
                    c = self.nextc()
                    if c == quote:
                        has_escaped_quote = 1
                    if c == 13:
                        c = self.nextc()
        return MK(token, STRING, self.start, self.cur)

    # ------------------------------------------------------------------
    # f-string mode
    # ------------------------------------------------------------------

    def _get_fstring_mode(self, current_tok, token):
        MK = self._make_token
        end_quote_size = 0
        unicode_escape = 0
        self.start = self.cur
        self.first_lineno = self.lineno
        self.starting_col_offset = self.col_offset

        start_char = self.nextc()
        if start_char == 123:
            peek1 = self.nextc()
            self.backup(peek1)
            self.backup(start_char)
            if peek1 != 123:
                current_tok.curly_bracket_expr_start_depth += 1
                if current_tok.curly_bracket_expr_start_depth >= MAX_EXPR_NESTING:
                    return MK(token, self._syntaxerror(
                        "%s-string: expressions nested too deeply" % self.string_prefix()),
                        None, None)
                self.mode().kind = TOK_REGULAR_MODE
                return self._get_normal_mode(current_tok, token)
        else:
            self.backup(start_char)

        # Check if we are at the end of the string
        at_end = True
        for i in range(current_tok.quote_size):
            quote = self.nextc()
            if quote != current_tok.quote:
                self.backup(quote)
                at_end = False
                break
        if at_end:
            current_tok.last_expr_start = None
            current_tok.last_expr_end = -1
            p_start = self.start
            p_end = self.cur
            self.tok_mode_stack_index -= 1
            return MK(token, TSTRING_END if current_tok.string_kind == TSTRING
                      else FSTRING_END, p_start, p_end)

        MIDDLE = TSTRING_MIDDLE if current_tok.string_kind == TSTRING else FSTRING_MIDDLE
        self.multi_line_start = self.line_start
        while end_quote_size != current_tok.quote_size:
            c = self.nextc()
            if self.done == E_ERROR or self.done == E_DECODE:
                return MK(token, ERRORTOKEN, None, None)
            in_format_spec = (current_tok.in_format_spec
                              and current_tok.curly_bracket_expr_start_depth >= 0)
            if c == EOF or (current_tok.quote_size == 1 and c == 10):
                if self.decoding_erred:
                    return MK(token, ERRORTOKEN, None, None)
                if in_format_spec and c == 10:
                    if current_tok.quote_size == 1:
                        return MK(token, self._syntaxerror(
                            "%s-string: newlines are not allowed in format specifiers "
                            "for single quoted %s-strings"
                            % (self.string_prefix(), self.string_prefix())), None, None)
                    self.backup(c)
                    self.mode().kind = TOK_REGULAR_MODE
                    current_tok.in_format_spec = 0
                    return MK(token, MIDDLE, self.start, self.cur)
                self.cur = current_tok.start
                self.cur += 1
                self.line_start = current_tok.multi_line_start
                start = self.lineno
                self.lineno = self.mode().first_line
                if current_tok.quote_size == 3:
                    self._syntaxerror("unterminated triple-quoted %s-string literal"
                                      " (detected at line %d)" % (self.string_prefix(), start))
                    if c != 10:
                        self.done = E_EOFS
                    return MK(token, ERRORTOKEN, None, None)
                else:
                    return MK(token, self._syntaxerror(
                        "unterminated %s-string literal (detected at line %d)"
                        % (self.string_prefix(), start)), None, None)

            if c == current_tok.quote:
                end_quote_size += 1
                continue
            else:
                end_quote_size = 0

            if c == 123:
                if not self._update_ftstring_expr(c):
                    return MK(token, ENDMARKER, None, None)
                peek = self.nextc()
                if peek != 123 or in_format_spec:
                    self.backup(peek)
                    self.backup(c)
                    current_tok.curly_bracket_expr_start_depth += 1
                    if current_tok.curly_bracket_expr_start_depth >= MAX_EXPR_NESTING:
                        return MK(token, self._syntaxerror(
                            "%s-string: expressions nested too deeply"
                            % self.string_prefix()), None, None)
                    self.mode().kind = TOK_REGULAR_MODE
                    current_tok.in_format_spec = 0
                    return MK(token, MIDDLE, self.start, self.cur)
                else:
                    return MK(token, MIDDLE, self.start, self.cur - 1)
            elif c == 125:
                if unicode_escape:
                    return MK(token, MIDDLE, self.start, self.cur)
                peek = self.nextc()
                cursor = current_tok.curly_bracket_depth
                if peek == 125 and not in_format_spec and cursor == 0:
                    return MK(token, MIDDLE, self.start, self.cur - 1)
                else:
                    self.backup(peek)
                    self.backup(c)
                    self.mode().kind = TOK_REGULAR_MODE
                    current_tok.in_format_spec = 0
                    return MK(token, MIDDLE, self.start, self.cur)
            elif c == 92:
                peek = self.nextc()
                if peek == 13:
                    peek = self.nextc()
                if peek == 123 or peek == 125:
                    if not current_tok.raw:
                        if self._warn_invalid_escape_sequence(peek):
                            return MK(token, ERRORTOKEN, None, None)
                    self.backup(peek)
                    continue
                if not current_tok.raw:
                    if peek == 78:  # 'N'
                        peek = self.nextc()
                        if peek == 123:
                            unicode_escape = 1
                        else:
                            self.backup(peek)
        # Backup the f-string quotes to emit a final FSTRING_MIDDLE and
        # add the quotes to the FSTRING_END in the next tokenizer iteration.
        for i in range(current_tok.quote_size):
            self.backup(current_tok.quote)
        return MK(token, MIDDLE, self.start, self.cur)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def get(self, token=None):
        """_PyTokenizer_Get: fill *token* and return its type."""
        if token is None:
            token = Token()
        current_tok = self.mode()
        if current_tok.kind == TOK_REGULAR_MODE:
            result = self._get_normal_mode(current_tok, token)
        else:
            result = self._get_fstring_mode(current_tok, token)
        if self.decoding_erred:
            result = ERRORTOKEN
            token.type = ERRORTOKEN
            self.done = E_DECODE
        return result

    def token_bytes(self, token):
        if token.start is None or token.end is None:
            return b''
        return bytes(self.data[token.start:token.end])


# ----------------------------------------------------------------------
# tokenize-module compatible front end (Python/Python-tokenize.c)
# ----------------------------------------------------------------------

class TokenInfo(collections.namedtuple('TokenInfo', 'type string start end line')):
    def __repr__(self):
        annotated_type = '%d (%s)' % (self.type, T.tok_name[self.type])
        return ('TokenInfo(type=%s, string=%r, start=%r, end=%r, line=%r)' %
                self._replace(type=annotated_type))

    @property
    def exact_type(self):
        if self.type == OP and self.string in T.EXACT_TOKEN_TYPES:
            return T.EXACT_TOKEN_TYPES[self.string]
        else:
            return self.type


class TokenError(Exception):
    pass


class _TokenizerIter:
    """Port of _tokenize.TokenizerIter."""

    def __init__(self, readline, extra_tokens=False, encoding=None):
        self.tok = TokState.from_readline(readline, encoding, 1, 1)
        self.tok.filename = "<string>"
        if extra_tokens:
            self.tok.tok_extra_tokens = 1
        self.done = 0
        self.last_line = None
        self.byte_col_offset_diff = 0
        self.last_lineno = 0
        self.last_end_lineno = 0

    def __iter__(self):
        return self

    def _tokenizer_error(self):
        tok = self.tok
        if tok.exc is not None:
            raise tok.exc
        errtype = SyntaxError
        done = tok.done
        if done == E_TOKEN:
            msg = "invalid token"
        elif done == E_EOF:
            e = SyntaxError("unexpected EOF in multi-line statement")
            e.filename = tok.filename
            e.lineno = tok.lineno
            e.offset = max(0, tok.inp - tok.buf)
            raise e
        elif done == E_DEDENT:
            msg = "unindent does not match any outer indentation level"
            errtype = IndentationError
        elif done == E_TABSPACE:
            errtype = TabError
            msg = "inconsistent use of tabs and spaces in indentation"
        elif done == E_TOODEEP:
            errtype = IndentationError
            msg = "too many levels of indentation"
        elif done == E_LINECONT:
            msg = "unexpected character after line continuation character"
        else:
            msg = "unknown tokenization error"
        size = tok.inp - tok.buf
        size -= 1
        error_line = bytes(tok.data[tok.buf:tok.buf + size]).decode('utf-8', 'replace')
        offset = byte_offset_to_character_offset(
            bytes(tok.data[tok.buf:tok.inp]), tok.inp - tok.buf)
        raise errtype(msg, (tok.filename, tok.lineno, offset, error_line, None, None))

    def __next__(self):
        tok = self.tok
        token = Token()
        type_ = tok.get(token)
        if type_ == ERRORTOKEN:
            self._tokenizer_error()
        if self.done or type_ == ERRORTOKEN:
            self.done = 1
            raise StopIteration("EOF")
        if token.start is None or token.end is None:
            s = ''
        else:
            s = bytes(tok.data[token.start:token.end]).decode('utf-8', 'surrogatepass')
        is_trailing_token = 0
        if type_ == ENDMARKER or (type_ == DEDENT and tok.done == E_EOF):
            is_trailing_token = 1
        line_start = tok.multi_line_start if is_string_lit(type_) else tok.line_start
        line_changed = 1
        if tok.tok_extra_tokens and is_trailing_token:
            line = ''
        else:
            size = tok.inp - line_start
            if size >= 1 and tok.implicit_newline:
                size -= 1
            if tok.lineno != self.last_lineno:
                line = bytes(tok.data[line_start:line_start + size]).decode('utf-8', 'replace')
                self.last_line = line
                self.byte_col_offset_diff = 0
            else:
                line = self.last_line
                line_changed = 0
        lineno = tok.first_lineno if is_string_lit(type_) else tok.lineno
        end_lineno = tok.lineno
        col_offset = -1
        end_col_offset = -1
        # _get_col_offsets
        byte_offset = -1
        line_bytes = bytes(tok.data[line_start:tok.inp])
        if token.start is not None and token.start >= line_start:
            byte_offset = token.start - line_start
            if line_changed:
                col_offset = byte_offset_to_character_offset_line(line_bytes, 0, byte_offset)
                self.byte_col_offset_diff = byte_offset - col_offset
            else:
                col_offset = byte_offset - self.byte_col_offset_diff
        if token.end is not None and token.end >= tok.line_start:
            end_byte_offset = token.end - tok.line_start
            if lineno == end_lineno:
                token_col_offset = byte_offset_to_character_offset_line(
                    line_bytes, byte_offset, end_byte_offset)
                end_col_offset = col_offset + token_col_offset
                self.byte_col_offset_diff += token.end - token.start - token_col_offset
            else:
                end_col_offset = byte_offset_to_character_offset(
                    bytes(tok.data[tok.line_start:]), end_byte_offset)
                self.byte_col_offset_diff += end_byte_offset - end_col_offset
        self.last_lineno = lineno
        self.last_end_lineno = end_lineno

        if tok.tok_extra_tokens:
            if is_trailing_token:
                lineno = end_lineno = lineno + 1
                col_offset = end_col_offset = 0
            if DEDENT < type_ < OP:
                type_ = OP
            elif type_ == NEWLINE:
                if not tok.implicit_newline:
                    if tok.data[tok.start] == 13:
                        s = "\r\n"
                    else:
                        s = "\n"
                else:
                    s = ''
                end_col_offset += 1
            elif type_ == NL:
                if tok.implicit_newline:
                    s = ''
        if type_ == ENDMARKER:
            self.done = 1
        return TokenInfo(type_, s, (lineno, col_offset), (end_lineno, end_col_offset), line)


def _transform_msg(msg):
    if "unterminated triple-quoted string literal" in msg:
        return "EOF in multi-line string"
    elif "unterminated string literal" in msg:
        return "EOL while scanning string literal"
    return msg


def _generate_tokens_from_c_tokenizer(source, encoding=None, extra_tokens=False):
    it = _TokenizerIter(source, extra_tokens=extra_tokens, encoding=encoding)
    try:
        for info in it:
            yield info
    except SyntaxError as e:
        if type(e) != SyntaxError:
            raise e from None
        msg = _transform_msg(str(e))
        raise TokenError(msg, (e.lineno, e.offset)) from None


def generate_tokens(readline):
    """Equivalent of tokenize.generate_tokens (str readline)."""
    return _generate_tokens_from_c_tokenizer(readline, extra_tokens=True)


def tokenize_bytes(readline, encoding='utf-8'):
    """Equivalent of tokenize.tokenize for a bytes readline whose encoding
    has already been detected (the ENCODING token is emitted first)."""
    yield TokenInfo(ENCODING, encoding, (0, 0), (0, 0), '')
    yield from _generate_tokens_from_c_tokenizer(readline, encoding, extra_tokens=True)


def tokenize_source(text):
    """Tokenize a str the way ``tokenize.generate_tokens`` would."""
    import io
    return generate_tokens(io.StringIO(text).readline)
