class A:
    """doc"""
    x = 1
    def __init__(self):
        self.a = 1
        self.b.c = 2
        self.d[0] = 3
    def m(self):
        return __class__
    def n(self):
        return super().n()
    def o(self, x):
        return super(A, self).o(x)
    def p(self):
        s = super
        return s().p()
    @property
    def q(self): return 1
    class Inner:
        def r(self): return __class__, A
    lst = [x for x in range(3)]
    lst2 = [y for y in range(3) if x]
    gen = (x for x in range(3))
    z = x
    __slots__ = ()
    __private = 1
    def __priv(self): return self.__private, self.__dict__
class B(A, metaclass=type, **kw): pass
class C(*bases, **kws): pass
@decorator
class D: pass
class E:
    x: int = 1
    y: "str"
    if cond:
        z: int
    def f(self) -> int: pass
class F:
    def f(self):
        class G:
            def g(self):
                return __class__, self
        return G
class H:
    x = 1
    def f(self, x=x): return x
    lst = [x for _ in range(3)]
class WithDel:
    def __del__(self): del self.x; del x
class Cond:
    if x:
        a: int = 1
    else:
        b: int
    for i in range(3):
        c: int
    while False:
        d: int
