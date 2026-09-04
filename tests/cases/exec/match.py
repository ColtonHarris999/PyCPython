match x:
    case 1: pass
    case 1 | 2 | 3: pass
    case "a" | b"b" | 1.5 | -1 | 1 + 2j | -1 - 2j | None | True | False: pass
    case [a, b, *rest]: pass
    case [*_, last]: pass
    case [first, *_]: pass
    case [first, *_, last]: pass
    case [_, _, *_]: pass
    case [*_]: pass
    case []: pass
    case (a, b): pass
    case {"k": v, **rest}: pass
    case {"k": v}: pass
    case {}: pass
    case {**rest}: pass
    case {Keys.K: v, 2: w}: pass
    case Cls(): pass
    case Cls(a, b): pass
    case Cls(a, k=b, l=c): pass
    case Cls(_, k=_): pass
    case Point(x=0, y=0) | Point(x=1, y=1): pass
    case [Point(x=0), *others] as whole: pass
    case a.b.c: pass
    case (1 | 2) as v if v > 1: pass
    case x if x: pass
    case _ if cond: pass
    case _: pass
match y:
    case _: pass
match z:
    case 1: pass
    case _: pass
match w:
    case a if a: pass
match (a, b):
    case (1, 2) | (2, 1): pass
    case ([x, y], [z]) | ([z], [x, y]): pass
def f(subject):
    match subject:
        case [int(a), str(b)] | [str(b), int(a)]:
            return a, b
        case {"x": [1, 2, *r]} | {"y": [*r, 1, 2]}:
            return r
        case Cls(a=1) as c:
            return c
    match subject:
        case 1 | 2:
            pass
        case [*_] | {}:
            pass
