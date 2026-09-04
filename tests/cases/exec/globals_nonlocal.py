def f():
    global a, b
    a = 1
    def g():
        nonlocal_x = 1
        def h():
            nonlocal nonlocal_x
            global c
            nonlocal_x += 1
            c = 2
        return h
    return g
def uses_global_then_local():
    global d
    d = 1
    def inner():
        d = 2
        return d
    return inner
class K:
    global e
    e = 1
    def m(self):
        global f
        f = 2
def import_global():
    global os
    import os
    return os
def del_global():
    global g
    del g
