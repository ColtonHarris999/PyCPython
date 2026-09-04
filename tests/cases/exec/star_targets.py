a, *b = c
*a, b = c
a, *b, c = d
[a, *b] = c
(a, *b), c = d
a, (b, *c) = d
for a, *b in c: pass
for *a, b in c: pass
with x as (a, *b): pass
[a, b], [c, d] = e
a = b = *c, d
(*a,) = b
[*a] = b
a[0], b.c = d
a.b, c[d] = e
