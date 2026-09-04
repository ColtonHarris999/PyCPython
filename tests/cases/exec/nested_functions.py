def a():
    def b():
        def c():
            def d():
                def e():
                    return x
                return e
            return d
        return c
    x = 1
    return b
def f(x):
    def g(y):
        def h(z):
            return x + y + z
        return h
    return g
def outer():
    v = 1
    class C:
        v = v
        def m(self):
            return v
    return C
def cells_and_frees(p):
    q = 1
    def inner(r):
        s = 2
        def inner2(t):
            return p, q, r, s, t
        return inner2
    return inner
def free_in_lambda(x):
    return lambda: x, lambda y: x + y, [lambda: x for _ in range(3)]
def generator_closure(x):
    def gen():
        yield x
    return gen
def modify_cell(x):
    def inner():
        nonlocal x
        x += 1
    return inner
def super_in_nested():
    class A:
        def m(self):
            def inner():
                return super().m()
            return inner
        def n(self):
            return lambda: super().n()
        def o(self):
            return [super().o() for _ in range(3)]
    return A
