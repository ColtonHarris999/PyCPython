try:
    pass
except A:
    x = 1
except B as e:
    del e
except C as e:
    e = 1
except (D, E):
    pass
except F as f:
    def g(): return f
except G as h:
    class K:
        z = h
except H as i:
    lst = [i for _ in range(3)]
try:
    pass
except:
    raise
try:
    pass
except A:
    try:
        pass
    except B:
        raise
    finally:
        pass
try:
    try:
        pass
    finally:
        pass
except:
    pass
try:
    pass
except* A as a:
    del a
except* (B, C):
    raise
except* D:
    pass
def f():
    try:
        return 1
    except A:
        return 2
    except B as e:
        return e
    else:
        return 3
    finally:
        return 4
def g():
    for i in x:
        try:
            pass
        except A:
            break
        except B:
            continue
        finally:
            pass
def h():
    try:
        try:
            raise A
        except A as e:
            raise B from e
    except B:
        pass
    try:
        x
    except A as a:
        try:
            y
        except B as b:
            return a, b
