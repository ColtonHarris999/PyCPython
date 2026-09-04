result = f(a)(b)(c)
result = (a
          .b(c)
          .d(e))
result = a.b.c.d.e
result = obj.method(
    arg1,
    arg2,
)
result = obj \
    .method(1)
x = a[b][c][d]
x = a[
    b
][c]
x = (a +
     b) * (c -
           d)
x = f(g(h(i)))
x = -a.b
x = a.b ** c.d
def gy():
    x = (yield a.b)
def f():
    return (a
            .b)
class C:
    def m(self):
        return (self
                .x
                .y)
