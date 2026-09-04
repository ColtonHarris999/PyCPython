"""Module docstring."""
import os, sys as system
from os import path, sep as separator
from . import sibling
from ..pkg import thing as other
x = 1
y: int = 2
z: list[int]
a, b = b, a = 1, 2
[c, *d] = range(5)
(e, (f, g)), *h = ((1, (2, 3)), 4, 5)
x += 1; x -= 1; x *= 2; x /= 2; x //= 1; x %= 3; x **= 2; x <<= 1; x >>= 1; x &= 1; x |= 1; x ^= 1; x @= 1
del x, y
del a[0], b.c, d[1:2]
assert x, "message"
assert (x, y)
global gg
print(x if y else z, not x, -x, +x, ~x, x ** -y)
lst = [1, 2, *d, 3]
tpl = (1, *d, 2)
st = {1, *d, 2}
dct = {1: 2, **other, 3: 4, **other}
big = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33]
bigt = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33)
bigd = {i: i for i in range(3)} | {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6, "g": 7, "h": 8, "i": 9, "j": 10, "k": 11, "l": 12, "m": 13, "n": 14, "o": 15, "p": 16}
call(1, 2, *d, k=3, **dct)
call(*d, *e)
call(**dct, **other)
call(a=1, *d)
x = a < b < c <= d > e >= f == g != h is i is not j in k not in l
x = 1 < 2
x = a and b or c and not d
x = lambda: 0
y = lambda a, /, b=1, *c, d, e=2, **f: (a, b, c, d, e, f)
x[1] = y[1:2] = z[::2] = w[1:2:3] = v[...] = u[1, 2] = t[1:2, 3] = 0
x = s[1], s[1:], s[:2], s[1:2], s[::-1], s[1:2:3], s[:], s[1:2, 3:4], s[..., None]
x = a.b.c.d
a.b.c = 5
x = f"plain {x} {y!r} {z:>10} {w!s:{width}} {{literal}} {x=} {x = !r:^5}"
x = t"template {x} {y!r:>{w}} literal"
x = b"bytes" b"more"
x = "implicit" "concat" f"{x}" "end"
x = 1_000_000 + 0x_ff + 0o17 + 0b1010 + 1.5e3 + 3j + 1.5J + .5 + 5. + 1e-10
x = -1 + -0.0 + -2j + (-1)**2
x = None, True, False, ...
x = "\N{DASH}" "\x41\u0041\U00000041" '\'' "\"" "tab\there"
x = r"raw\n" rb"raw\n" Rb"x" BR"y" u"unicode"
