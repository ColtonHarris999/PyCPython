x: int = 1
y: str
z: list[int] = []
if cond:
    c: int = 1
else:
    d: str
for i in range(3):
    e: int
class A:
    a: int
    b: str = "x"
    def f(self, x: int, *args: int, y: str = "", **kw: dict) -> None: pass
    if cond:
        c: int
    class B:
        d: int
        def g(self) -> "B": pass
    e: (x for x in range(3))
def fn(a: int, /, b: str, *c: float, d: list, **e) -> tuple: pass
def fn2(a: "forward" = 1): pass
def fn3(*args: *Ts): pass
def generic[T](a: T, b: list[T]) -> T: pass
class G[T]:
    x: T
    def m(self, a: T) -> T: pass
lambda a: a
w.a: int
v[0]: int
(u): int = 1
def inner():
    x: int = 1
    y: str
    def deeper(a: int): pass
    class C:
        z: int
