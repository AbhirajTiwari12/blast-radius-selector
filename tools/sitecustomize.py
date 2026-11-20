"""Per-test tracer: which lines ran inside functions, which ran at import (module/class bodies),
which classes were instantiated, and which data files under tests/ were opened."""
import os, sys, json, atexit
_out = os.environ.get("TRACE_OUT")
_root = os.environ.get("TRACE_ROOT", "")
if _out:
    mon = sys.monitoring
    TOOL = 3
    mon.use_tool_id(TOOL, "blast")
    func, imp, classes, opened = {}, {}, {}, set()
    keep = {}
    def want(fn):
        r = keep.get(fn)
        if r is None:
            r = keep[fn] = fn.startswith(_root) and "/site-packages/" not in fn
        return r
    def on_line(code, line):
        fn = code.co_filename
        if want(fn):
            (func if code.co_flags & 1 else imp).setdefault(fn, set()).add(line)
        return mon.DISABLE
    def on_start(code, off):
        if code.co_name != "__init__" and code.co_name != "__new__":
            return mon.DISABLE
        try:
            f = sys._getframe(1)
            if f.f_code is code and code.co_argcount:
                obj = f.f_locals.get(code.co_varnames[0])
                c = obj if isinstance(obj, type) else type(obj)
                if c.__module__.startswith(("sqlglot", "tests")):
                    k = c.__module__ + ":" + c.__qualname__
                    if k not in classes:
                        classes[k] = [b.__module__ + ":" + b.__qualname__ for b in c.__mro__[1:] if b.__module__.startswith(("sqlglot", "tests"))]
        except Exception:
            pass
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
    def hook(ev, args):
        if ev == "open":
            a = args[0]
            if isinstance(a, str) and "/tests/" in a and "__pycache__" not in a and not a.endswith((".py", ".pyc")):
                opened.add(a)
    sys.addaudithook(hook)
    def dump():
        mon.set_events(TOOL, 0)
        n = len(_root)
        json.dump({"func": {k[n:]: sorted(v) for k, v in func.items()},
                   "imp": {k[n:]: sorted(v) for k, v in imp.items()},
                   "classes": classes, "opened": sorted(a[n:] for a in opened if a.startswith(_root))},
                  open(_out + "." + str(os.getpid()), "w"), separators=(",", ":"))
    atexit.register(dump)
    try:
        import multiprocessing.util as _mu
        _mu.Finalize(None, dump, exitpriority=0)
        _mu.register_after_fork(dump, lambda d: _mu.Finalize(None, d, exitpriority=0))
    except Exception:
        pass
