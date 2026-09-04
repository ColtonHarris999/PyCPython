def f():
    with a:
        return 1
    with a as b:
        with c as d:
            return b, d
    with a, b:
        pass
    for i in x:
        with y:
            break
        with z:
            continue
    while x:
        with y:
            break
    with a:
        try:
            return
        finally:
            pass
    with a:
        try:
            pass
        except:
            return
    with a:
        while x:
            break
    with a:
        for i in x:
            return i
    try:
        with a:
            pass
    finally:
        pass
async def g():
    async with a:
        return 1
    async with a as b:
        async with c:
            return b
    for i in x:
        async with y:
            break
