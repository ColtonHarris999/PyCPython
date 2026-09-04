def f(a, b=1, *args, c, d=2, **kwargs) -> int:
    """Docstring
       with indentation
    """
    return a + b
def g(a, /, b, *, c): pass
def h(*, a): pass
def i(a, /): pass
async def co(x):
    await x
    async for i in x: pass
    async with x as y, z: pass
    return [i async for i in x]
def gen():
    yield
    yield 1
    x = yield 2
    yield from range(3)
    return 5
async def agen():
    yield 1
    x = yield
def nested(a):
    b = 1
    def inner(c):
        nonlocal b
        b += c
        return a + b + c
    def inner2():
        return inner
    class K:
        def m(self):
            return a + b + super().m()
    return inner, inner2, K
@decorator
@decorator.attr(1, 2)
def decorated(): pass
def defaults(a=x, b=(1, 2), c={}, d=[], e=None, *, f=lambda: 1): pass
def annotated(a: int, b: "str" = 1, *c: float, d: list[int], **e: dict) -> None: pass
def with_inner_annots():
    x: int = 1
    y: str
    z.a: int = 2
    w[0]: int
def many_locals(a, b):
    l1 = l2 = l3 = l4 = l5 = l6 = l7 = l8 = l9 = l10 = 0
    l11 = l12 = l13 = l14 = l15 = l16 = l17 = l18 = l19 = l20 = 0
    l21 = l22 = l23 = l24 = l25 = l26 = l27 = l28 = l29 = l30 = 0
    l31 = l32 = l33 = l34 = l35 = l36 = l37 = l38 = l39 = l40 = 0
    l41 = l42 = l43 = l44 = l45 = l46 = l47 = l48 = l49 = l50 = 0
    l51 = l52 = l53 = l54 = l55 = l56 = l57 = l58 = l59 = l60 = 0
    l61 = l62 = l63 = l64 = l65 = l66 = l67 = l68 = l69 = l70 = 0
    del l5
    if a: l71 = 1
    return l1 + l70 + l71 + l5
def returns():
    if x: return
    if y: return 1
    for i in x:
        try:
            return i
        finally:
            print()
    while x:
        with y:
            return
    return (1,
            2)
def conditional_expr(x):
    return 1 if x else 2, (yield)
def star_args_call(f, a, b):
    f(*a, *b, **a, **b)
    f(a, *b, c=1, **b)
    f(*(a, b))
