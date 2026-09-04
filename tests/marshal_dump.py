"""Debug tool: print a marshal byte stream as a flat token list (offset, type,
FLAG_REF, payload summary), so two dumps can be diffed with `diff`."""
import struct
import sys

FLAG_REF = 0x80


class Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.out = []
        self.nrefs = 0

    def r(self, n):
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def r_long(self):
        return struct.unpack('<i', self.r(4))[0]

    def emit(self, off, code, flag, payload):
        self.out.append("%6d %s%s %s" % (off, chr(code), '*' if flag else ' ', payload))

    def obj(self, depth=0):
        off = self.pos
        b = self.r(1)[0]
        flag = b & FLAG_REF
        code = b & ~FLAG_REF
        c = chr(code)
        refidx = None
        if flag:
            refidx = self.nrefs
            self.nrefs += 1
        tag = ("ref#%d" % refidx) if flag else ""
        if c in '0NFTS.':
            self.emit(off, code, flag, tag)
        elif c == 'i':
            self.emit(off, code, flag, "%s %d" % (tag, self.r_long()))
        elif c == 'l':
            n = self.r_long()
            self.r(2 * abs(n))
            self.emit(off, code, flag, "%s long(%d digits)" % (tag, n))
        elif c == 'g':
            self.emit(off, code, flag, "%s %r" % (tag, struct.unpack('<d', self.r(8))[0]))
        elif c == 'y':
            self.emit(off, code, flag, "%s %r" % (tag, struct.unpack('<dd', self.r(16))))
        elif c in 'sutaAzZ':
            if c in 'zZ':
                n = self.r(1)[0]
            else:
                n = self.r_long()
            s = self.r(n)
            self.emit(off, code, flag, "%s %r" % (tag, s[:60]))
        elif c in '()<>':
            n = self.r(1)[0] if c == ')' else self.r_long()
            self.emit(off, code, flag, "%s n=%d" % (tag, n))
            for _ in range(n):
                self.obj(depth + 1)
        elif c == '{':
            self.emit(off, code, flag, tag)
            while self.data[self.pos] != ord('0'):
                self.obj(depth + 1)
                self.obj(depth + 1)
            self.obj(depth + 1)
        elif c == 'r':
            self.emit(off, code, flag, "%s ->%d" % (tag, self.r_long()))
        elif c == 'c':
            argcount = self.r_long(); posonly = self.r_long(); kwonly = self.r_long()
            stacksize = self.r_long(); flags = self.r_long()
            self.emit(off, code, flag, "%s code argc=%d posonly=%d kwonly=%d stack=%d flags=%#x"
                      % (tag, argcount, posonly, kwonly, stacksize, flags))
            for name in ('code', 'consts', 'names', 'localsplusnames', 'localspluskinds',
                         'filename', 'name', 'qualname'):
                self.out.append("       .%s" % name)
                self.obj(depth + 1)
            self.out.append("       .firstlineno %d" % self.r_long())
            for name in ('linetable', 'exceptiontable'):
                self.out.append("       .%s" % name)
                self.obj(depth + 1)
        else:
            raise ValueError("unknown marshal type %r at %d" % (c, off))


def dump(path):
    data = open(path, 'rb').read()
    rd = Reader(data)
    rd.obj()
    return rd.out


if __name__ == '__main__':
    for line in dump(sys.argv[1]):
        print(line)
