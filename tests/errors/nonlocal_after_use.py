def f():
    x = 1
    def g():
        y = x
        nonlocal x
