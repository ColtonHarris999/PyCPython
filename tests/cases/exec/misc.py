if __debug__: x = 1
assert __debug__
x = __debug__
def f(): return __debug__
y = not __debug__
z = [__debug__ for _ in range(3)]
pass
...
"string statement"
1
x
x.y
x[0]
x()
(x)
[x]
{x}
{x: y}
x + 1
del x
x = y = z = 1
x, y = 1, 2
(x, y) = [1, 2]
[x, y] = (1, 2)
x = *a, *b
x = [*a, *b]
x = (*a,)
*x, = a
x, *y = a
x = a if b else c if d else e
x = not not a
x = --a
x = a is not b
x = a == b == c
x = a[b][c]
x = a(b)(c)
x = a.b(c).d
x = a[b:c] + a[b:c:d] + a[:] + a[b:] + a[:c]
a[b:c] = 1
del a[b:c]
a[b:c] += 1
a[1] += 1
a.b += 1
a.b.c += 1
a[b][c] += 1
a().b += 1
x = -1 * 2
x = 2 ** 3 ** 2
x = (2 ** 3) ** 2
x = 1 if 1 else 2
x = (1, 2) if (3, 4) else (5, 6)
x = [1, 2][0]
x = {1: 2}[1]
x = (1, 2, 3)[1:]
x = "abc" * 3
x = 1 in (1, 2, 3)
x = 1 not in [1, 2, 3]
x = 1 in {1, 2, 3}
x = a in ()
x = a in []
x = a in {}
x = a in "str"
x = a in b"bytes"
x = (a, b, c) == (1, 2, 3)
x = not a in b
x = not a is b
x = a and b and c
x = a or b or c
x = (a and b) or c
x = a and (b or c)
x = a if (b and c) else d
x = [i for i in a and b]
x = a, = 1,
for x, in y: pass
for (x, y) in z: pass
for [x, y] in z: pass
for x.y in z: pass
for x[0] in z: pass
lambda x=1, *, y=2: x + y
lambda *a, **k: 0
lambda a, b, c: 0
lambda: [x for x in range(3)]
x = lambda a=x: a
def f():
    return import a.b.c
import a.b.c as d
from a.b import c
from a import *
from . import a
from .a import b
from ... import c
from .... import d
from ...a.b import c as d
import a, b.c as d, e
