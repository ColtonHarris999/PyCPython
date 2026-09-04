x = 1
def f():
    global x
    x = 2
    def g():
        nonlocal_test = 1
        def h():
            nonlocal nonlocal_test
            nonlocal_test = 2
        return h
    return g
def uses_class_cell():
    class C:
        def m(self): return __class__
    return C
def cell_arg(a, b):
    def inner(): return a
    return inner
def free_and_local(a):
    def inner():
        a = 1
        def inner2(): return a
        return inner2
    return inner
class Outer:
    y = 1
    def m(self):
        return y
    class Inner:
        z = y
        def n(self): return z
    def with_comp(self):
        return [y for _ in range(3)]
def type_params[T, *Ts, **P](a: T, *b: *Ts, c: P.args = 1) -> T:
    def inner[U](x: U) -> U: return x
    class K[V]: pass
    type Alias[W] = list[W]
    return a
class Generic[T: int, *V, **W, U = str]:
    def m(self) -> T: pass
    type A = T
type Simple = int
type Bounded[T: (int, str)] = list[T]
type WithDefault[T = int] = T
def annotated_generic[T](x: list[T]) -> dict[str, T]: pass
def lambda_scopes():
    return lambda: (lambda: x)
def comprehension_cells():
    a = [lambda: x for x in range(3)]
    b = [x for x in range(3) if (y := x)]
    return a, b, y
def del_locals(a):
    del a
    a = 1
    return a
def conditional_del(a, c):
    if c: del a
    return a
def loop_locals():
    for i in range(3): pass
    return i
def try_locals():
    try: x = 1
    except E as e: pass
    return x, e
def with_dunder_class(self):
    __class__ = 1
    return __class__
class Mangled:
    def f(self, __a, __b=1, *__c, __d, **__e):
        __x = __a
        return self.__y, __x, __a.__z, __import__
    __z: int = 1
    def __g(self): return self.__g, __g
    import __os__
    from __m__ import __n__
    for __i in range(3): pass
    __slots__ = ("__a",)
    kw = dict(__k=1)
    lst = [__v for __v in range(3)]
    lam = lambda __l: __l
