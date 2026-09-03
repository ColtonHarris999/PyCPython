"""Port of Parser/string_parser.c plus the escape decoders from
Objects/unicodeobject.c (_PyUnicode_DecodeUnicodeEscapeInternal2) and
Objects/bytesobject.c (_PyBytes_DecodeEscape2).
"""

import unicodedata
import warnings

from .. import tokens as T

MAX_UNICODE = 0x10FFFF


class DecodeError(Exception):
    """Carries the exception CPython would have set (UnicodeDecodeError or
    ValueError) so that callers can convert it the way the C code does."""

    def __init__(self, exc):
        super().__init__(str(exc))
        self.exc = exc


def decode_unicode_escape(s):
    """Return (text, first_invalid_escape_char, first_invalid_escape_ptr).

    *s* is bytes.  Raises DecodeError wrapping a UnicodeDecodeError on
    malformed escapes (strict error handling)."""
    out = []
    first_invalid_escape_char = -1
    first_invalid_escape_ptr = None
    n = len(s)
    i = 0
    while i < n:
        c = s[i]
        i += 1
        if c != 92:
            out.append(chr(c))
            continue
        startinpos = i - 1
        if i >= n:
            raise DecodeError(UnicodeDecodeError('unicodeescape', bytes(s), startinpos, n,
                                                 "\\ at end of string"))
        c = s[i]
        i += 1
        if c == 10:
            continue
        if c == 92:
            out.append('\\')
            continue
        if c == 39:
            out.append("'")
            continue
        if c == 34:
            out.append('"')
            continue
        if c == 98:
            out.append('\b')
            continue
        if c == 102:
            out.append('\014')
            continue
        if c == 116:
            out.append('\t')
            continue
        if c == 110:
            out.append('\n')
            continue
        if c == 114:
            out.append('\r')
            continue
        if c == 118:
            out.append('\013')
            continue
        if c == 97:
            out.append('\007')
            continue
        if 48 <= c <= 55:
            ch = c - 48
            if i < n and 48 <= s[i] <= 55:
                ch = (ch << 3) + s[i] - 48
                i += 1
                if i < n and 48 <= s[i] <= 55:
                    ch = (ch << 3) + s[i] - 48
                    i += 1
            if ch > 0o377:
                if first_invalid_escape_char == -1:
                    first_invalid_escape_char = ch
                    first_invalid_escape_ptr = i - 3
            out.append(chr(ch))
            continue
        if c in (120, 117, 85):  # x u U
            if c == 120:
                count = 2
                message = "truncated \\xXX escape"
            elif c == 117:
                count = 4
                message = "truncated \\uXXXX escape"
            else:
                count = 8
                message = "truncated \\UXXXXXXXX escape"
            ch = 0
            while count:
                if i >= n:
                    raise DecodeError(UnicodeDecodeError('unicodeescape', bytes(s),
                                                         startinpos, n, message))
                cc = s[i]
                ch <<= 4
                if 48 <= cc <= 57:
                    ch += cc - 48
                elif 97 <= cc <= 102:
                    ch += cc - 87
                elif 65 <= cc <= 70:
                    ch += cc - 55
                else:
                    raise DecodeError(UnicodeDecodeError('unicodeescape', bytes(s),
                                                         startinpos, i, message))
                i += 1
                count -= 1
            if ch > MAX_UNICODE:
                raise DecodeError(UnicodeDecodeError('unicodeescape', bytes(s), startinpos, i,
                                                     "illegal Unicode character"))
            out.append(chr(ch))
            continue
        if c == 78:  # N
            message = "malformed \\N character escape"
            if i >= n:
                raise DecodeError(UnicodeDecodeError('unicodeescape', bytes(s), startinpos, n,
                                                     message))
            if s[i] == 123:
                i += 1
                start = i
                while i < n and s[i] != 125:
                    i += 1
                if i >= n:
                    raise DecodeError(UnicodeDecodeError('unicodeescape', bytes(s),
                                                         startinpos, n, message))
                namelen = i - start
                if namelen:
                    i += 1
                    name = bytes(s[start:start + namelen])
                    ch = None
                    try:
                        looked = unicodedata.lookup(name.decode('latin-1'))
                        if len(looked) == 1:
                            ch = looked
                    except (KeyError, UnicodeError):
                        ch = None
                    if ch is not None:
                        out.append(ch)
                        continue
                    message = "unknown Unicode character name"
            raise DecodeError(UnicodeDecodeError('unicodeescape', bytes(s), startinpos, i,
                                                 message))
        # default: invalid escape
        if first_invalid_escape_char == -1:
            first_invalid_escape_char = c
            first_invalid_escape_ptr = i - 1
        out.append('\\')
        out.append(chr(c))
    return ''.join(out), first_invalid_escape_char, first_invalid_escape_ptr


_HEXDIGITS = b'0123456789abcdefABCDEF'


def _digit_value(c):
    if 48 <= c <= 57:
        return c - 48
    if 97 <= c <= 122:
        return c - 87
    if 65 <= c <= 90:
        return c - 55
    return 37


def decode_bytes_escape(s):
    """_PyBytes_DecodeEscape2 with strict errors.  Returns
    (bytes, first_invalid_escape_char, first_invalid_escape_ptr)."""
    out = bytearray()
    first_invalid_escape_char = -1
    first_invalid_escape_ptr = None
    n = len(s)
    i = 0
    while i < n:
        c = s[i]
        if c != 92:
            out.append(c)
            i += 1
            continue
        i += 1
        if i == n:
            raise DecodeError(ValueError("Trailing \\ in string"))
        c = s[i]
        i += 1
        if c == 10:
            pass
        elif c == 92:
            out.append(92)
        elif c == 39:
            out.append(39)
        elif c == 34:
            out.append(34)
        elif c == 98:
            out.append(8)
        elif c == 102:
            out.append(12)
        elif c == 116:
            out.append(9)
        elif c == 110:
            out.append(10)
        elif c == 114:
            out.append(13)
        elif c == 118:
            out.append(11)
        elif c == 97:
            out.append(7)
        elif 48 <= c <= 55:
            v = c - 48
            if i < n and 48 <= s[i] <= 55:
                v = (v << 3) + s[i] - 48
                i += 1
                if i < n and 48 <= s[i] <= 55:
                    v = (v << 3) + s[i] - 48
                    i += 1
            if v > 0o377:
                if first_invalid_escape_char == -1:
                    first_invalid_escape_char = v
                    first_invalid_escape_ptr = i - 3
            out.append(v & 0xFF)
        elif c == 120:
            if i + 1 < n:
                d1 = _digit_value(s[i])
                d2 = _digit_value(s[i + 1])
                if d1 < 16 and d2 < 16:
                    out.append((d1 << 4) + d2)
                    i += 2
                    continue
            raise DecodeError(ValueError("invalid \\x escape at position %d" % (i - 2)))
        else:
            if first_invalid_escape_char == -1:
                first_invalid_escape_char = c
                first_invalid_escape_ptr = i - 1
            out.append(92)
            i -= 1
    return bytes(out), first_invalid_escape_char, first_invalid_escape_ptr


def warn_invalid_escape_sequence(p, buffer, first_invalid_escape, t):
    """Port of warn_invalid_escape_sequence (string_parser.c)."""
    if p.call_invalid_rules:
        return 0
    c = buffer[first_invalid_escape]
    if (t.type in (T.FSTRING_MIDDLE, T.FSTRING_END, T.TSTRING_MIDDLE, T.TSTRING_END)
            and c in (123, 125)):
        return 0
    octal = 52 <= c <= 55
    if octal:
        seq = bytes(buffer[first_invalid_escape:first_invalid_escape + 3]).decode('latin-1')
        msg = ('"\\%s" is an invalid octal escape sequence. '
               'Such sequences will not work in the future. '
               'Did you mean "\\\\%s"? A raw string is also an option.' % (seq, seq))
    else:
        ch = chr(c)
        msg = ('"\\%s" is an invalid escape sequence. '
               'Such sequences will not work in the future. '
               'Did you mean "\\\\%s"? A raw string is also an option.' % (ch, ch))
    if p.feature_version >= 12:
        category = SyntaxWarning
    else:
        category = DeprecationWarning
    lineno = t.lineno
    col_offset = t.col_offset
    for k in range(first_invalid_escape):
        if buffer[k] == 10:
            lineno += 1
            col_offset = 0
        else:
            col_offset += 1
    if lineno == t.lineno:
        quote_count = 0
        first_quote = 0
        for b in t.bytes:
            if b == 39 or b == 34:
                if quote_count == 0:
                    first_quote = b
                if b == first_quote:
                    quote_count += 1
            else:
                break
        col_offset += quote_count
    try:
        warnings.warn_explicit(msg, category, p.tok.filename, lineno)
    except category:
        p.known_err_token = t
        if octal:
            p.raise_error_known_location(
                SyntaxError, lineno, col_offset - 1, lineno, col_offset + 1,
                '"\\%s" is an invalid octal escape sequence. '
                'Did you mean "\\\\%s"? A raw string is also an option.' % (seq, seq))
        else:
            p.raise_error_known_location(
                SyntaxError, lineno, col_offset - 1, lineno, col_offset + 1,
                '"\\%s" is an invalid escape sequence. '
                'Did you mean "\\\\%s"? A raw string is also an option.' % (ch, ch))
    return 0


def decode_unicode_with_escapes(p, s, t):
    """Port of decode_unicode_with_escapes.  *s* is bytes (UTF-8)."""
    buf = bytearray()
    n = len(s)
    i = 0
    while i < n:
        if s[i] == 92:
            buf.append(92)
            i += 1
            if i >= n or s[i] & 0x80:
                buf.extend(b"u005c")
                if i >= n:
                    break
        if s[i] & 0x80:
            j = i
            while j < n and s[j] & 0x80:
                j += 1
            try:
                w = bytes(s[i:j]).decode('utf-8')
            except UnicodeDecodeError as e:
                raise DecodeError(e)
            for ch in w:
                buf.extend(("\\U%08x" % ord(ch)).encode('ascii'))
            i = j
        else:
            buf.append(s[i])
            i += 1
    v, first_invalid_escape_char, first_invalid_escape_ptr = decode_unicode_escape(bytes(buf))
    if first_invalid_escape_ptr is not None and t is not None:
        warn_invalid_escape_sequence(p, buf, first_invalid_escape_ptr, t)
    return v


def decode_bytes_with_escapes(p, s, t):
    result, first_invalid_escape_char, first_invalid_escape_ptr = decode_bytes_escape(s)
    if first_invalid_escape_ptr is not None:
        warn_invalid_escape_sequence(p, s, first_invalid_escape_ptr, t)
    return result


def decode_string(p, raw, s, t):
    """_PyPegen_decode_string: *s* is bytes."""
    if raw:
        try:
            return bytes(s).decode('utf-8')
        except UnicodeDecodeError as e:
            raise DecodeError(e)
    return decode_unicode_with_escapes(p, s, t)


def parse_string(p, t):
    """_PyPegen_parse_string: decode a STRING token into str or bytes."""
    s = t.bytes
    i = 0
    quote = s[0]
    bytesmode = 0
    rawmode = 0
    if (65 <= quote <= 90) or (97 <= quote <= 122):
        while not bytesmode or not rawmode:
            if quote == 98 or quote == 66:
                i += 1
                quote = s[i]
                bytesmode = 1
            elif quote == 117 or quote == 85:
                i += 1
                quote = s[i]
            elif quote == 114 or quote == 82:
                i += 1
                quote = s[i]
                rawmode = 1
            else:
                break
    if quote != 39 and quote != 34:
        raise SystemError("bad call to parse_string")
    i += 1
    body = s[i:]
    length = len(body)
    if body[length - 1] != quote:
        raise SystemError("bad call to parse_string")
    length -= 1
    if length >= 4 and body[0] == quote and body[1] == quote:
        body = body[2:]
        length -= 2
        length -= 1
        if body[length] != quote:
            raise SystemError("bad call to parse_string")
        length -= 1
        if body[length] != quote:
            raise SystemError("bad call to parse_string")
    body = body[:length]
    rawmode = rawmode or (92 not in body)
    if bytesmode:
        for ch in body:
            if ch >= 0x80:
                p.raise_syntax_error_known_location(
                    t, "bytes can only contain ASCII literal characters")
        if rawmode:
            return bytes(body)
        return decode_bytes_with_escapes(p, body, t)
    return decode_string(p, rawmode, body, t)
