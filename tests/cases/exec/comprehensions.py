a = [x for x in y]
b = [x for x in y if x if x > 1]
c = [x + y for x in z for y in w]
d = [x for x in [1]]
e = [x for x in (1,)]
f = {x for x in y}
g = {x: y for x, y in z}
h = (x for x in y)
i = [(x, y) for x in a if x for y in b if y]
j = [[y for y in x] for x in z]
k = [x for x in y if (z := x)]
l = [lambda: x for x in y]
o = {x: [y for y in x] for x in z}
p = sum(x for x in y)
q = any(x for x in y)
r = all(x for x in y)
s = tuple(x for x in y)
t = list(x for x in y)
u = any([x for x in y])
def fn(y):
    a = [x for x in y]
    b = {x for x in y}
    x = 1
    c = [x for x in y]
    d = [x for x in y if x]
    e = [[x for x in y] for y in z]
    def inner():
        return [x for x in y]
    f = [inner for inner in y]
    g = [y for y in y]
    del x
    return a, x
class K:
    y = 1
    a = [x for x in range(3)]
    b = [y for x in range(3)]
    c = [K for x in range(3)]
    d = {x: y for x in range(3)}
    e = (x for x in range(3))
    def m(self):
        return [self for _ in range(3)]
async def af(y):
    a = [x async for x in y]
    b = [x for x in y if await x]
    c = {x async for x in y}
    d = (x async for x in y)
    e = [x for x in y for z in await x]
    return a
def gen(y):
    yield [x for x in y]
    return (x for x in y)
