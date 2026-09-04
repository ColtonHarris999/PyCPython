def f():
    x = 1
    def g():
        nonlocal x
        x: int
