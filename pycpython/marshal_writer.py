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
                 'exceptiontable')

    def __init__(self, argcount, posonlyargcount, kwonlyargcount, stacksize, flags, code,
                 consts, names, localsplusnames, localspluskinds, filename, name, qualname,
                 firstlineno, linetable, exceptiontable):
        self.argcount = argcount
        self.posonlyargcount = posonlyargcount
        self.kwonlyargcount = kwonlyargcount
        self.stacksize = stacksize
        self.flags = flags
        self.code = code
        self.consts = consts
        self.names = names
        self.localsplusnames = localsplusnames
        self.localspluskinds = localspluskinds
        self.filename = filename
        self.name = name
        self.qualname = qualname
        self.firstlineno = firstlineno
        self.linetable = linetable
        self.exceptiontable = exceptiontable


class ConstCache(dict):
    """The compiler's constant cache (compile.c c_const_cache) plus the
    bookkeeping needed to preserve object sharing across the marshal round
    trips used to build code objects: ``alias`` maps id(object created by
    marshal.loads) -> the original object it was serialized from."""

    def __init__(self):
        super().__init__()
        self.alias = {}
        self.keep = []
        # ids of frozenset constants that CPython's intern_constants() would
        # rebuild (see emulate_code_interning): never shared between code
        # objects.
        self.rebuild = set()


class Writer:
    def __init__(self, alias=None):
        self.out = []
        self.refs = {}
        self.nrefs = 0
        self.keepalive = []
        self.alias = alias if alias is not None else {}
        # ids of str objects that init_code() interns in place (co_name,
        # co_qualname, co_filename); the same object may also appear as a
        # constant (e.g. a class body's __qualname__ constant).
        self.interned_ids = set()
        self.rebuild = set()

    def scan_interned(self, v):
        t = type(v)
        if t is CodeSpec:
            for x in (v.filename, v.name, v.qualname):
                self.interned_ids.add(id(self.alias.get(id(x), x)))
            self.scan_interned(v.consts)
        elif t is _code_type:
            for x in (v.co_filename, v.co_name, v.co_qualname):
                self.interned_ids.add(id(self.alias.get(id(x), x)))
            self.scan_interned(v.co_consts)
        elif t is tuple:
            for x in v:
                if type(x) in (tuple, CodeSpec, _code_type):
                    self.scan_interned(x)

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
        canon = self.alias.get(id(v), v)
        idx = self.refs.get(id(canon))
        if idx is not None:
            self.w_byte(TYPE_REF[0])
            self.w_long(idx)
            return True
        self.refs[id(canon)] = self.nrefs
        self.nrefs += 1
        self.keepalive.append(canon)
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
        elif type(v) is frozenset and id(self.alias.get(id(v), v)) in self.rebuild:
            # written as a fresh, unshared object (no reference entry)
            self.w_complex_object(v, 0, str_kind)
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
                canon = self.alias.get(id(v), v)
                interned = should_intern_string(v) or is_interned(v)
                if not interned and id(canon) in self.interned_ids:
                    # init_code() interns the co_name/co_qualname/co_filename
                    # object in place unless an equal interned string
                    # already exists (then only the code attribute is
                    # redirected and this constant stays non-interned).
                    interned = sys.intern(canon) is canon
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
                            linetable=v.co_linetable, exceptiontable=v.co_exceptiontable)
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
        # init_code() interns co_filename, co_name and co_qualname (mortal,
        # in place when possible).  When an equal interned string already
        # exists the attribute ends up pointing at that other object, so
        # write a standalone interned string instead of a reference.
        for x in (co.filename, co.name, co.qualname):
            canon = self.alias.get(id(x), x)
            if sys.intern(canon) is canon:
                self.w_object(x, 'name')
            else:
                self.w_str(x, 0, True)
        self.w_long(co.firstlineno)
        self.w_object(co.linetable)
        self.w_object(co.exceptiontable)


_code_type = type((lambda: 0).__code__)


def emulate_code_interning(spec, cache):
    """Mirror what _PyCode_New() does to the strings of a new code object
    (intern_strings(names), intern_constants(consts),
    intern_strings(localsplusnames)), in the same order, using
    sys.intern() so that the interpreter's interned-string table evolves
    exactly as it would under the C compiler.

    The observable consequence we need: a frozenset constant whose
    identifier-like string items get *replaced* by already-interned equal
    strings is rebuilt (a new frozenset object per code object), so it is
    never shared between code objects."""
    for n in spec.names:
        sys.intern(n)
    _intern_constants(spec.consts, cache, None)
    for n in spec.localsplusnames:
        sys.intern(n)


def _intern_constants(t, cache, modified):
    """Returns True if a direct str item of *t* was replaced."""
    result = False
    for i in range(len(t) - 1, -1, -1):
        v = t[i]
        tv = type(v)
        if tv is str:
            if should_intern_string(v):
                c = sys.intern(v)
                if c is not v:
                    result = True
        elif tv is tuple:
            _intern_constants(v, cache, None)
        elif tv is frozenset:
            canon = cache.alias.get(id(v), v)
            if _intern_constants(tuple(v), cache, True):
                cache.rebuild.add(id(canon))
                cache.keep.append(canon)
    return result


def build_code(spec, cache):
    """Turn a CodeSpec (possibly containing code objects built earlier by
    this module) into a real code object, recording in *cache* how the
    objects created by marshal.loads map to the originals."""
    emulate_code_interning(spec, cache)
    w = Writer(cache.alias)
    w.rebuild = cache.rebuild
    w.scan_interned(spec)
    w.w_object(spec)
    data = b''.join(w.out)
    co = marshal.loads(data)
    _register(co, spec, cache)
    return co


def _alias(loaded, orig, cache):
    if loaded is orig:
        return
    t = type(orig)
    if t is CodeSpec:
        _register(loaded, orig, cache)
        return
    canon = cache.alias.get(id(orig), orig)
    cache.alias[id(loaded)] = canon
    cache.keep.append(loaded)
    cache.keep.append(canon)
    if t is tuple:
        for a, b in zip(loaded, orig):
            _alias(a, b, cache)
    elif t is frozenset:
        if len(loaded) == len(orig):
            by_key = {}
            for b in orig:
                by_key.setdefault(_ckey(b), []).append(b)
            for a in loaded:
                lst = by_key.get(_ckey(a))
                if lst:
                    _alias(a, lst.pop(), cache)
    elif t is slice:
        _alias(loaded.start, orig.start, cache)
        _alias(loaded.stop, orig.stop, cache)
        _alias(loaded.step, orig.step, cache)
    elif t is _code_type:
        extra = _CODE_EXTRA.get(orig)
        if extra is not None:
            _CODE_EXTRA[loaded] = extra
        _alias_code_fields(loaded, orig, cache)


def _ckey(o):
    from .codegen import constant_key
    return constant_key(o)


def _code_fields(co):
    return (co.co_code, co.co_consts, co.co_names, co.co_filename, co.co_name,
            co.co_qualname, co.co_linetable, co.co_exceptiontable)


def _alias_code_fields(loaded, orig, cache):
    for a, b in zip(_code_fields(loaded), _code_fields(orig)):
        _alias(a, b, cache)
    extra_l = _CODE_EXTRA.get(loaded)
    extra_o = _CODE_EXTRA.get(orig)
    if extra_l is not None and extra_o is not None:
        _alias(extra_l[0], extra_o[0], cache)


def _register(co, spec, cache):
    _CODE_EXTRA[co] = (spec.localsplusnames, spec.localspluskinds)
    spec_fields = (spec.code, spec.consts, spec.names, spec.filename, spec.name,
                   spec.qualname, spec.linetable, spec.exceptiontable)
    for a, b in zip(_code_fields(co), spec_fields):
        _alias(a, b, cache)
    _alias(_CODE_EXTRA[co][0], spec.localsplusnames, cache)
