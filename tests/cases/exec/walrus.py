if (n := len(a)) > 10: pass
while (chunk := f.read()): pass
x = [y := f(), y ** 2]
z = [(v := i) for i in range(3)]
w = {(k := i): (m := i) for i in range(3)}
def f():
    return (a := 1), (b := [c := 2 for _ in range(3)]), a, b, c
def g():
    if any((x := i) for i in range(3)): return x
class C:
    y = [i for i in range(3)]
lambda: (p := 1)
print((r := 1), r)
t = ((s := 1), s)
