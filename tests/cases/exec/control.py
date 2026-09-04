for i in range(10):
    if i == 1: continue
    if i == 2: break
    for j in i:
        if j: break
        else: continue
    else:
        pass
else:
    x = 1
while x:
    x -= 1
    if x: break
    while y:
        continue
else:
    y = 2
if a:
    pass
elif b:
    pass
elif c:
    x = 1
else:
    x = 2
if a and b or not c: pass
if a and (b or c): pass
if not (a or b): pass
if a if b else c: pass
if (x := f()) and (y := g()): pass
if a < b < c: pass
if not a < b < c: pass
if a in b: pass
if a not in b: pass
if a is None: pass
if a is not None: pass
if a in [1, 2, 3]: pass
if a in {1, 2, 3}: pass
if a in (1, 2, 3): pass
if a in [x, y]: pass
if a in {x, y}: pass
for i in [1, 2, 3]: pass
for i in {1, 2, 3}: pass
for i in (1, 2, 3): pass
for i in [x, y]: pass
for i in {x, y}: pass
for i in [x]: pass
for i in []: pass
for i, j in [(1, 2)]: pass
for i in range(3): pass
while True: break
while 1: break
while 0: pass
while False: pass
if True: pass
if False: pass
if None: pass
if 1: pass
if 0: pass
if "": pass
if "x": pass
if ...: pass
if x is True: pass
try:
    pass
except ValueError:
    pass
except (TypeError, KeyError) as e:
    pass
except:
    pass
else:
    pass
finally:
    pass
try:
    x
finally:
    y
try:
    x
except* ValueError as e:
    pass
except* TypeError:
    pass
else:
    pass
finally:
    pass
try:
    x
except* Error:
    pass
with a: pass
with a as b: pass
with a as b, c as d: pass
with (a as b, c as d): pass
with a, b: pass
raise
raise ValueError
raise ValueError from None
raise ValueError("x") from e
def f():
    for i in x:
        try:
            if i: return i
            elif j: break
            else: continue
        except E:
            if i: return
            break
        finally:
            if i: return 1
            print()
    try:
        pass
    finally:
        return 2
    with a:
        return 3
    try:
        try:
            pass
        except:
            pass
    finally:
        pass
    try:
        with a:
            try:
                return 1
            finally:
                pass
    except* E:
        pass
    return 4
def g():
    while x:
        try:
            continue
        finally:
            break
    for i in x:
        with a:
            try:
                continue
            except:
                break
            else:
                continue
            finally:
                pass
def h(gen):
    for x in gen:
        yield x
        try:
            yield
        finally:
            pass
async def k():
    async for i in x:
        try:
            await i
        finally:
            break
    async with a as b:
        return b
    return await x
