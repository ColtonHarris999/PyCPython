async def f(x):
    async with x as a, y as b:
        pass
    async for i in x:
        pass
    else:
        pass
    await x
    return await y
async def g():
    return [i async for i in x], {i: j async for i, j in y}, (i async for i in z)
async def h():
    async with a:
        return
    async with a:
        async for i in b:
            break
        else:
            pass
async def agen():
    yield
    async for i in x:
        yield i
    try:
        yield 1
    finally:
        pass
    async with a:
        yield 2
def sync_gen_in_async():
    async def inner():
        yield
    return inner
async def deep():
    try:
        async with a:
            try:
                await b
            except E:
                await c
            finally:
                await d
    except* F:
        await e
