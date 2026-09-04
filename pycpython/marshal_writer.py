"""A writer for the ``marshal`` serialization format, used to construct
code objects.

``types.CodeType`` cannot express every part of a code object that the
compiler produces (the per-local "kinds" byte: argument kinds and hidden
comprehension locals), whereas ``marshal.loads`` feeds the serialized
fields straight into the code constructor.  So the assembler serializes
the code object it wants with this writer and reads it back with
``marshal.loads``.  Object sharing is preserved through reference entries.

Only the object types that can appear in code objects are supported."""

import marshal
import struct
import sys
import weakref

TYPE_NULL = b'0'
TYPE_NONE = b'N'
TYPE_FALSE = b'F'
TYPE_TRUE = b'T'
TYPE_ELLIPSIS = b'.'
TYPE_INT = b'i'
TYPE_LONG = b'l'
TYPE_BINARY_FLOAT = b'g'
TYPE_BINARY_COMPLEX = b'y'
TYPE_STRING = b's'
TYPE_INTERNED = b't'
TYPE_REF = b'r'
TYPE_TUPLE = b'('
TYPE_FROZENSET = b'>'
TYPE_CODE = b'c'
TYPE_UNICODE = b'u'
TYPE_ASCII = b'a'
TYPE_ASCII_INTERNED = b'A'
TYPE_SMALL_TUPLE = b')'
TYPE_SHORT_ASCII = b'z'
TYPE_SHORT_ASCII_INTERNED = b'Z'
TYPE_SLICE = b':'
FLAG_REF = 0x80

# code object -> (localsplusnames, localspluskinds) for code objects built
# by this library (needed to re-serialize them as nested constants).
_CODE_EXTRA = weakref.WeakKeyDictionary()

# value -> the str object CPython would hold as an interned identifier.
INTERNED = {}


def intern_identifier(s):
    s = sys.intern(s)
    INTERNED[s] = s
    return s


def should_intern_string(s):
    """codeobject.c should_intern_string: [a-zA-Z0-9_]*, ASCII only."""
    if not s.isascii():
        return False
    for ch in s:
        if not (ch.isalnum() or ch == '_'):
            return False
    return True


def is_interned(s):
    return INTERNED.get(s) is s


class CodeSpec:
    """The fields of a code object to be constructed."""
    __slots__ = ('argcount', 'posonlyargcount', 'kwonlyargcount', 'stacksize', 'flags',
                 'code', 'consts', 'names', 'localsplusnames', 'localspluskinds',
                 'filename', 'name', 'qualname', 'firstlineno', 'linetable',
                 'exceptiontable', 'name_interned')

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class Writer:
    def __init__(self):
        self.out = []
        self.refs = {}
        self.nrefs = 0
        self.keepalive = []

    def w_byte(self, b):
        self.out.append(bytes((b,)))

    def w_long(self, x):
        self.out.append(struct.pack('<i', x))

    def w_type(self, t, flag):
        self.out.append(bytes((t[0] | flag,)))

    def w_size(self, n):
        self.w_long(n)

    def w_ref(self, v):
        """Returns True if a reference was written."""
        idx = self.refs.get(id(v))
        if idx is not None:
            self.w_byte(TYPE_REF[0])
            self.w_long(idx)
            return True
        self.refs[id(v)] = self.nrefs
        self.nrefs += 1
        self.keepalive.append(v)
        return False

    def w_pstring(self, b):
        self.w_size(len(b))
        self.out.append(b)

    def w_short_pstring(self, b):
        self.w_byte(len(b))
        self.out.append(b)

    def w_str(self, s, flag, interned):
        if s.isascii():
            data = s.encode('ascii')
            if len(s) < 256:
                self.w_type(TYPE_SHORT_ASCII_INTERNED if interned else TYPE_SHORT_ASCII, flag)
                self.w_short_pstring(data)
            else:
                self.w_type(TYPE_ASCII_INTERNED if interned else TYPE_ASCII, flag)
                self.w_pstring(data)
        else:
            data = s.encode('utf-8', 'surrogatepass')
            self.w_type(TYPE_INTERNED if interned else TYPE_UNICODE, flag)
            self.w_pstring(data)

    def w_pylong(self, v, flag):
        self.w_type(TYPE_LONG, flag)
        sign = -1 if v < 0 else 1
        v = abs(v)
        digits = []
        while v:
            digits.append(v & 0x7FFF)
            v >>= 15
        self.w_long(sign * len(digits))
        for d in digits:
            self.out.append(struct.pack('<H', d))

    def w_object(self, v, str_kind='const'):
        if v is None:
            self.w_byte(TYPE_NONE[0])
        elif v is Ellipsis:
            self.w_byte(TYPE_ELLIPSIS[0])
        elif v is False:
            self.w_byte(TYPE_FALSE[0])
        elif v is True:
            self.w_byte(TYPE_TRUE[0])
        elif self.w_ref(v):
            pass
        else:
            self.w_complex_object(v, FLAG_REF, str_kind)

    def w_complex_object(self, v, flag, str_kind):
        t = type(v)
        if t is int:
            if -(1 << 31) <= v < (1 << 31):
                self.w_type(TYPE_INT, flag)
                self.w_long(v)
            else:
                self.w_pylong(v, flag)
        elif t is float:
            self.w_type(TYPE_BINARY_FLOAT, flag)
            self.out.append(struct.pack('<d', v))
        elif t is complex:
            self.w_type(TYPE_BINARY_COMPLEX, flag)
            self.out.append(struct.pack('<dd', v.real, v.imag))
        elif t is bytes:
            self.w_type(TYPE_STRING, flag)
            self.w_pstring(v)
        elif t is str:
            if str_kind == 'name':
                interned = True
            elif str_kind == 'const':
                interned = should_intern_string(v) or is_interned(v)
            elif str_kind == 'plain':
                interned = False
            else:
                interned = bool(str_kind)
            self.w_str(v, flag, interned)
        elif t is tuple:
            n = len(v)
            if n < 256:
                self.w_type(TYPE_SMALL_TUPLE, flag)
                self.w_byte(n)
            else:
                self.w_type(TYPE_TUPLE, flag)
                self.w_size(n)
            for item in v:
                self.w_object(item, str_kind)
        elif t is frozenset:
            self.w_type(TYPE_FROZENSET, flag)
            self.w_size(len(v))
            for item in v:
                self.w_object(item, str_kind)
        elif t is slice:
            self.w_type(TYPE_SLICE, flag)
            self.w_object(v.start, str_kind)
            self.w_object(v.stop, str_kind)
            self.w_object(v.step, str_kind)
        elif t is CodeSpec:
            self.w_codespec(v, flag)
        elif t is _code_type:
            extra = _CODE_EXTRA.get(v)
            if extra is None:
                raise TypeError("cannot re-serialize a code object not built by pycpython")
            localsplusnames, kinds = extra
            spec = CodeSpec(argcount=v.co_argcount, posonlyargcount=v.co_posonlyargcount,
                            kwonlyargcount=v.co_kwonlyargcount, stacksize=v.co_stacksize,
                            flags=v.co_flags, code=v.co_code, consts=v.co_consts,
                            names=v.co_names, localsplusnames=localsplusnames,
                            localspluskinds=kinds, filename=v.co_filename, name=v.co_name,
                            qualname=v.co_qualname, firstlineno=v.co_firstlineno,
                            linetable=v.co_linetable, exceptiontable=v.co_exceptiontable,
                            name_interned=not v.co_name.startswith("<generic parameters of "))
            self.w_codespec(spec, flag)
        else:
            raise TypeError("unmarshallable object of type %s" % t.__name__)

    def w_codespec(self, co, flag):
        self.w_type(TYPE_CODE, flag)
        self.w_long(co.argcount)
        self.w_long(co.posonlyargcount)
        self.w_long(co.kwonlyargcount)
        self.w_long(co.stacksize)
        self.w_long(co.flags)
        self.w_object(co.code)
        self.w_object(co.consts, 'const')
        self.w_object(co.names, 'name')
        self.w_object(co.localsplusnames, 'name')
        # The kinds bytes object is created fresh for every code object in
        # CPython (never merged through the constant cache), so it must not
        # be shared even when equal (1-byte bytes objects are singletons).
        self.w_type(TYPE_STRING, 0)
        self.w_pstring(co.localspluskinds)
        self.w_object(co.filename, 'plain')
        self.w_object(co.name, 'name' if co.name_interned else 'plain')
        if co.qualname is co.name:
            self.w_object(co.qualname, 'plain')  # written as a reference
        else:
            self.w_object(co.qualname, 'plain')
        self.w_long(co.firstlineno)
        self.w_object(co.linetable)
        self.w_object(co.exceptiontable)


_code_type = type((lambda: 0).__code__)


def build_code(spec):
    """Turn a CodeSpec (possibly containing code objects built earlier by
    this module) into a real code object."""
    w = Writer()
    w.w_object(spec)
    data = b''.join(w.out)
    co = marshal.loads(data)
    _register(co, spec)
    return co


def _register(co, spec):
    _CODE_EXTRA[co] = (tuple(spec.localsplusnames), bytes(spec.localspluskinds))
    # nested code objects were re-created by marshal.loads: register them too
    for c, s in zip(co.co_consts, spec.consts):
        if type(c) is _code_type:
            if type(s) is _code_type:
                extra = _CODE_EXTRA.get(s)
                if extra is not None:
                    _CODE_EXTRA[c] = extra
                    _register_nested(c, s)
            elif type(s) is CodeSpec:
                _register(c, s)


def _register_nested(c, s):
    for cc, ss in zip(c.co_consts, s.co_consts):
        if type(cc) is _code_type and type(ss) is _code_type:
            extra = _CODE_EXTRA.get(ss)
            if extra is not None:
                _CODE_EXTRA[cc] = extra
                _register_nested(cc, ss)
