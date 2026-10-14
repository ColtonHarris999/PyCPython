"""The ``.mpy`` container format of MicroPython v1.29.0 (``.mpy`` version 6).

This is a port of the reader and writer in ``py/persistentcode.c``, pinned to
tag ``v1.29.0``.  It decodes an ``.mpy`` file into plain Python structures and
re-encodes them byte for byte; the compiler's emitter writes its output
through ``encode()``.

Two properties are deliberate and load-bearing:

* **No recursion.**  ``save_obj`` (nested tuples) and ``save_raw_code``
  (nested functions) both recurse in C over structures whose depth is bounded
  only by the input.  Everything here walks an explicit stack held in the
  heap instead, so the module's own call depth is constant.  The target's
  call stack is a fixed pool of hardware-managed frames and the whole
  compiler has a 32-frame budget; see ``tools/lint_frames.py``.
* **Byte-exactness.**  ``encode(decode(data)) == data`` for every file the
  oracle produces.  ``tests/check_roundtrip.py`` asserts this over a corpus.

The code runs on MicroPython v1.29.0 as well as CPython, so it avoids
f-strings, ``enum``, ``typing``, ``functools``, ``itertools`` and the string
methods absent from the pin (``isalnum``, ``isidentifier``, ``expandtabs``).
"""

# -- header ----------------------------------------------------------------

MPY_VERSION = 6
MPY_SUB_VERSION = 3

# py/persistentcode.h: the sub-version occupies the low 2 bits of the feature
# byte and the architecture the bits above it.  mp_raw_code_save() writes both
# only when the module carries native code, so a bytecode-only file -- the
# only kind this compiler produces -- has feature byte 0.  The loader checks
# the sub-version solely when the architecture nibble is not "none", so 0x00
# and 0x03 both load; 0x00 is what the oracle writes and therefore what
# byte-exactness requires.
FEATURE_BYTE_BYTECODE_ONLY = 0x00

MP_NATIVE_ARCH_NONE = 0
MPY_FEATURE_ARCH_FLAGS = 0x02


def feature_decode_sub_version(feat):
    return feat & 3


def feature_decode_arch(feat):
    return (feat >> 2) & 0x2F


# -- raw code kinds (py/emitglue.h) ---------------------------------------

MP_CODE_UNUSED = 0
MP_CODE_RESERVED = 1
MP_CODE_BYTECODE = 2
MP_CODE_NATIVE_PY = 3
MP_CODE_NATIVE_VIPER = 4
MP_CODE_NATIVE_ASM = 5

# -- constant object kinds (py/persistentcode.h) --------------------------

OBJ_FUN_TABLE = 0
OBJ_NONE = 1
OBJ_FALSE = 2
OBJ_TRUE = 3
OBJ_ELLIPSIS = 4
OBJ_STR = 5
OBJ_BYTES = 6
OBJ_INT = 7
OBJ_FLOAT = 8
OBJ_COMPLEX = 9
OBJ_TUPLE = 10

# Objects carrying no payload at all.
_OBJ_EMPTY = (OBJ_FUN_TABLE, OBJ_NONE, OBJ_FALSE, OBJ_TRUE, OBJ_ELLIPSIS)
# Objects stored as a length then that many bytes plus a NUL terminator.
_OBJ_NUL_TERMINATED = (OBJ_STR, OBJ_BYTES)
# Numbers, stored as their textual repr with no terminator.
_OBJ_TEXT = (OBJ_INT, OBJ_FLOAT, OBJ_COMPLEX)

# The last qstr in the static pool (py/persistentcode.c: MP_QSTR_zip).  A qstr
# at or below it is written as its pool index rather than as its text.
QSTR_LAST_STATIC = None  # filled in by mpyc.qstrs once generated


class MpyError(Exception):
    pass


# -- variable-length unsigned integers ------------------------------------
#
# mp_print_uint / mp_decode_uint: base-128, most significant group first,
# every byte but the last carrying 0x80.

def encode_uint(n, out):
    """Append *n* to the bytearray *out*."""
    if n < 0:
        raise MpyError("cannot encode negative length")
    if n < 0x80:
        out.append(n)
        return
    groups = []
    groups.append(n & 0x7F)
    n >>= 7
    while n:
        groups.append(0x80 | (n & 0x7F))
        n >>= 7
    i = len(groups) - 1
    while i >= 0:
        out.append(groups[i])
        i -= 1


def decode_uint(data, pos):
    """Return ``(value, new_pos)``."""
    n = 0
    while True:
        if pos >= len(data):
            raise MpyError("truncated varint")
        b = data[pos]
        pos += 1
        n = (n << 7) | (b & 0x7F)
        if not (b & 0x80):
            return n, pos


# -- qstr table entries ----------------------------------------------------
#
# A static qstr is written as ``uint(index << 1 | 1)``; any other qstr as
# ``uint(len << 1)`` followed by its bytes and a NUL.

class StaticQstr:
    """A reference to entry *index* of the interpreter's static qstr pool."""

    def __init__(self, index):
        self.index = index

    def __eq__(self, other):
        return isinstance(other, StaticQstr) and other.index == self.index

    def __repr__(self):
        return "StaticQstr(%d)" % self.index


def encode_qstr(q, out):
    if isinstance(q, StaticQstr):
        encode_uint(q.index << 1 | 1, out)
        return
    encode_uint(len(q) << 1, out)
    out.extend(q)
    out.append(0)


def decode_qstr(data, pos):
    head, pos = decode_uint(data, pos)
    if head & 1:
        return StaticQstr(head >> 1), pos
    n = head >> 1
    if pos + n + 1 > len(data):
        raise MpyError("truncated qstr")
    text = bytes(data[pos:pos + n])
    if data[pos + n] != 0:
        raise MpyError("qstr missing NUL terminator")
    return text, pos + n + 1


# -- constant objects ------------------------------------------------------

class Obj:
    """One entry of the ``.mpy`` constant object table.

    ``kind`` is one of the ``OBJ_*`` codes.  ``payload`` is ``None`` for the
    empty kinds, a ``bytes`` for strings, bytes and numbers (numbers are
    stored as their textual repr, as in ``save_obj``), or a list of ``Obj``
    for a tuple.
    """

    def __init__(self, kind, payload=None):
        self.kind = kind
        self.payload = payload

    def __eq__(self, other):
        return (isinstance(other, Obj) and other.kind == self.kind
                and other.payload == self.payload)

    def __repr__(self):
        return "Obj(%d, %r)" % (self.kind, self.payload)


def encode_obj(obj, out):
    """Append *obj* to *out*, iteratively -- tuples nest without recursion."""
    # Each stack entry is [obj, next_child_index]; a tuple is revisited until
    # its children are written.
    stack = [[obj, -1]]
    while stack:
        top = stack[-1]
        node = top[0]
        if top[1] == -1:
            out.append(node.kind)
            if node.kind in _OBJ_EMPTY:
                stack.pop()
                continue
            if node.kind in _OBJ_NUL_TERMINATED:
                encode_uint(len(node.payload), out)
                out.extend(node.payload)
                out.append(0)
                stack.pop()
                continue
            if node.kind in _OBJ_TEXT:
                encode_uint(len(node.payload), out)
                out.extend(node.payload)
                stack.pop()
                continue
            if node.kind != OBJ_TUPLE:
                raise MpyError("unknown object kind %d" % node.kind)
            encode_uint(len(node.payload), out)
            top[1] = 0
        if node.kind == OBJ_TUPLE:
            i = top[1]
            if i >= len(node.payload):
                stack.pop()
                continue
            top[1] = i + 1
            stack.append([node.payload[i], -1])


def decode_obj(data, pos):
    """Return ``(Obj, new_pos)``, iteratively."""
    root = None
    # Entries are [tuple_obj, remaining_children]; children are appended to
    # the parent's payload list as they are decoded.
    stack = []
    while True:
        if pos >= len(data):
            raise MpyError("truncated object")
        kind = data[pos]
        pos += 1
        if kind in _OBJ_EMPTY:
            node = Obj(kind)
        elif kind in _OBJ_NUL_TERMINATED:
            n, pos = decode_uint(data, pos)
            if pos + n + 1 > len(data):
                raise MpyError("truncated object payload")
            node = Obj(kind, bytes(data[pos:pos + n]))
            if data[pos + n] != 0:
                raise MpyError("object missing NUL terminator")
            pos += n + 1
        elif kind in _OBJ_TEXT:
            n, pos = decode_uint(data, pos)
            if pos + n > len(data):
                raise MpyError("truncated number")
            node = Obj(kind, bytes(data[pos:pos + n]))
            pos += n
        elif kind == OBJ_TUPLE:
            n, pos = decode_uint(data, pos)
            node = Obj(kind, [])
            if root is None:
                root = node
            if n:
                if stack:
                    stack[-1][0].payload.append(node)
                stack.append([node, n])
                continue
            # an empty tuple is complete as it stands
        else:
            raise MpyError("unknown object kind %d" % kind)

        if root is None:
            root = node
        if not stack:
            return root, pos
        stack[-1][0].payload.append(node)
        # Close out any parents whose children are all present.
        while stack:
            top = stack[-1]
            if len(top[0].payload) < top[1]:
                break
            stack.pop()
        if not stack:
            return root, pos


# -- raw code tree ---------------------------------------------------------

class RawCode:
    """One function's compiled code.

    ``fun_data`` is the opaque byte blob holding the prelude, the bytecode and
    the line-number table -- opaque *at this layer*; the emitter builds it and
    ``mpyc.bytecode`` will decode it.  ``children`` are the nested functions.
    """

    def __init__(self, kind_delta, fun_data, children=None):
        self.kind_delta = kind_delta
        self.fun_data = fun_data
        self.children = children if children is not None else []

    def __eq__(self, other):
        return (isinstance(other, RawCode) and other.kind_delta == self.kind_delta
                and other.fun_data == self.fun_data
                and other.children == self.children)

    def __repr__(self):
        return "RawCode(kind_delta=%d, %d bytes, %d children)" % (
            self.kind_delta, len(self.fun_data), len(self.children))


def encode_raw_code(rc, out):
    """Append the raw-code tree rooted at *rc* to *out*, iteratively."""
    stack = [[rc, -1]]
    while stack:
        top = stack[-1]
        node = top[0]
        if top[1] == -1:
            n_children = len(node.children)
            encode_uint((len(node.fun_data) << 3)
                        | ((1 if n_children else 0) << 2)
                        | node.kind_delta, out)
            out.extend(node.fun_data)
            if node.kind_delta != 0:
                raise MpyError("only bytecode raw codes are supported "
                               "(kind_delta=%d)" % node.kind_delta)
            if n_children:
                encode_uint(n_children, out)
            top[1] = 0
        i = top[1]
        if i >= len(node.children):
            stack.pop()
            continue
        top[1] = i + 1
        stack.append([node.children[i], -1])


def decode_raw_code(data, pos):
    """Return ``(RawCode, new_pos)``, iteratively."""
    root = None
    stack = []  # [node, remaining_children]
    while True:
        head, pos = decode_uint(data, pos)
        kind_delta = head & 3
        has_children = (head >> 2) & 1
        fun_data_len = head >> 3
        if pos + fun_data_len > len(data):
            raise MpyError("truncated function data")
        node = RawCode(kind_delta, bytes(data[pos:pos + fun_data_len]))
        pos += fun_data_len
        if kind_delta != 0:
            raise MpyError("native raw code is not supported (kind_delta=%d)"
                           % kind_delta)
        if root is None:
            root = node
        if stack:
            stack[-1][0].children.append(node)
        if has_children:
            n, pos = decode_uint(data, pos)
            stack.append([node, n])
            continue
        if not stack:
            return root, pos
        while stack:
            top = stack[-1]
            if len(top[0].children) < top[1]:
                break
            stack.pop()
        if not stack:
            return root, pos


# -- whole file ------------------------------------------------------------

class MpyFile:
    """A decoded ``.mpy`` module."""

    def __init__(self, small_int_bits, qstrs, objs, raw_code,
                 feature_byte=FEATURE_BYTE_BYTECODE_ONLY, arch_flags=0):
        self.small_int_bits = small_int_bits
        self.qstrs = qstrs
        self.objs = objs
        self.raw_code = raw_code
        self.feature_byte = feature_byte
        self.arch_flags = arch_flags

    def __repr__(self):
        return "MpyFile(small_int_bits=%d, %d qstrs, %d objs)" % (
            self.small_int_bits, len(self.qstrs), len(self.objs))


def encode(mpy):
    """Serialize *mpy* to ``bytes``."""
    out = bytearray()
    out.append(0x4D)  # 'M'
    out.append(MPY_VERSION)
    out.append(mpy.feature_byte)
    out.append(mpy.small_int_bits)
    if mpy.arch_flags:
        encode_uint(mpy.arch_flags, out)
    encode_uint(len(mpy.qstrs), out)
    encode_uint(len(mpy.objs), out)
    for q in mpy.qstrs:
        encode_qstr(q, out)
    for o in mpy.objs:
        encode_obj(o, out)
    encode_raw_code(mpy.raw_code, out)
    return bytes(out)


def decode(data):
    """Parse an ``.mpy`` file.  Raises ``MpyError`` on anything unexpected."""
    if len(data) < 4:
        raise MpyError("file too short to hold a header")
    if data[0] != 0x4D:
        raise MpyError("not an .mpy file (bad magic)")
    if data[1] != MPY_VERSION:
        raise MpyError("unsupported .mpy version %d (expected %d)"
                       % (data[1], MPY_VERSION))
    feature_byte = data[2]
    small_int_bits = data[3]
    pos = 4
    arch = feature_decode_arch(feature_byte)
    if arch != MP_NATIVE_ARCH_NONE:
        raise MpyError("native .mpy files are not supported (arch %d)" % arch)
    arch_flags = 0
    if feature_byte & MPY_FEATURE_ARCH_FLAGS:
        arch_flags, pos = decode_uint(data, pos)
    n_qstr, pos = decode_uint(data, pos)
    n_obj, pos = decode_uint(data, pos)
    qstrs = []
    for _ in range(n_qstr):
        q, pos = decode_qstr(data, pos)
        qstrs.append(q)
    objs = []
    for _ in range(n_obj):
        o, pos = decode_obj(data, pos)
        objs.append(o)
    raw_code, pos = decode_raw_code(data, pos)
    if pos != len(data):
        raise MpyError("%d trailing bytes after the outer raw code"
                       % (len(data) - pos))
    return MpyFile(small_int_bits, qstrs, objs, raw_code,
                   feature_byte=feature_byte, arch_flags=arch_flags)
