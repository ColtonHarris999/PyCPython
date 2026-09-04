@a
@b.c
@d(1)
@e[0]
@(f or g)
@lambda f: f
def fn(): pass
@a
@b
class K: pass
def defaults(a=1, b=[], c=(x for x in y), d=lambda: e, *, f=g(), h=(1, 2, 3)): pass
def kwdefaults(*, a=1, b=2, c=3): pass
def mixed(a, b=1, *c, d=2, e, **f): pass
class M:
    def __init__(self, x=y, *, z=w): pass
    lam = lambda self, a=b: a
