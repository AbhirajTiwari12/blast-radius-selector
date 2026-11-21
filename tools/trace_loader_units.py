"""Unit traces for tests that loop over the entries of fixture files (pinned commit).

usage: trace_loader_units.py TESTS_JSON OUT_DIR FIRST_INDEX N_CHUNKS MIN_SECONDS
       (cwd = sqlglot checkout, SKIP_INTEGRATION=0)
A test that reads its cases through tests.helpers.load_sql_fixture_pairs / load_sql_fixtures is
run N_CHUNKS times, each time with the loaders yielding only one contiguous share of every
file's entries. Each run is traced like sitecustomize.py does (plus what import and the class
fixture ran). Prints "<n>\t<test id>[<chunk>]\t<cpu ms>" lines and a JSON summary on stderr."""
import json, os, sys, time, unittest, importlib, pickle
import concurrent.futures as cf, concurrent.futures.process as cfp

if __name__ != "__main__":
    raise SystemExit
tests_json, out, first, nchunks, minsec = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5])
root = os.getcwd() + "/"
sys.path.insert(0, root)

class InlinePool:  # the selector runs pooled work in-process too
    def __init__(self, *a, initializer=None, initargs=(), **k):
        self._init = (initializer, initargs); self._started = False
    def submit(self, fn, /, *args, **kwargs):
        f = cf.Future()
        try:
            if not self._started:
                self._started = True
                if self._init[0]: self._init[0](*pickle.loads(pickle.dumps(self._init[1])))
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
        except Exception as e:
            f.set_exception(e)
        return f
    def shutdown(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *e): return False
cf.ProcessPoolExecutor = cfp.ProcessPoolExecutor = InlinePool

mon = sys.monitoring
TOOL = 3
mon.use_tool_id(TOOL, "blast-u")
func, imp, classes = {}, {}, {}
def on_line(code, line):
    fn = code.co_filename
    if fn.startswith(root) and "/site-packages/" not in fn:
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
def snap():
    n = len(root)
    return ({k[n:]: set(v) for k, v in func.items()}, {k[n:]: set(v) for k, v in imp.items()}, dict(classes))
def reset():
    func.clear(); imp.clear(); classes.clear(); mon.restart_events()

LOADERS = ("load_sql_fixture_pairs", "load_sql_fixtures")
tests = [t for t in json.load(open(tests_json))["tests"] if t["seconds"] >= minsec]
groups = {}
for t in tests:
    f, c, m = t["id"].split("::")
    groups.setdefault((f, c), []).append((m, t))
n = first
summary = {}
devnull = open(os.devnull, "w")
for (f, c), items in sorted(groups.items()):
    mod = importlib.import_module(f[:-3].replace("/", "."))
    if not any(hasattr(mod, a) for a in LOADERS):
        continue
    cls = getattr(mod, c)
    origs = {a: getattr(mod, a) for a in LOADERS if hasattr(mod, a)}
    used = []
    allow = [None]
    def make(a):
        def loader(filename):
            used.append(filename)
            items_ = list(origs[a](filename))
            if allow[0] is None:
                yield from items_
                return
            total = len(items_)
            for i, it in enumerate(items_):
                if (i * nchunks) // total == allow[0]:
                    yield it
        return loader
    for a in origs:
        setattr(mod, a, make(a))
    so, se = sys.stdout, sys.stderr
    try:
        cls.setUpClass()
    except Exception:
        for a, o in origs.items(): setattr(mod, a, o)
        continue
    pre = snap()
    for m, t in sorted(items):
        def run():
            r = unittest.TestResult()
            sys.stdout = sys.stderr = devnull
            try:
                case = cls(m)
                case.run(r)   # single test: no class fixture handling (already set up)
            finally:
                sys.stdout, sys.stderr = so, se
            return not (r.errors or r.failures)
        used.clear(); allow[0] = None
        ok = run()
        if not used or not ok:
            continue
        cpus = []
        for ch in range(nchunks):
            allow[0] = ch
            reset()
            t0 = time.process_time()
            ok = run()
            cpus.append(time.process_time() - t0)
            fu, im, cl = snap()
            for k, v in pre[0].items(): fu.setdefault(k, set()).update(v)
            for k, v in pre[1].items(): im.setdefault(k, set()).update(v)
            cl.update(pre[2])
            json.dump({"func": {k: sorted(v) for k, v in fu.items()}, "imp": {k: sorted(v) for k, v in im.items()},
                       "classes": cl, "opened": []}, open("%s/%d.json" % (out, n), "w"), separators=(",", ":"))
            print("%d\t%s[%d]\t%d\t%d" % (n, t["id"], ch, cpus[-1] * 1000, int(ok)), flush=True)
            n += 1
        allow[0] = None
        tot = sum(cpus) or 1
        summary[t["id"]] = {"on": "loader", "attr": [a for a in origs], "n": nchunks, "units": [str(i) for i in range(nchunks)],
                            "ms": {str(i): max(1, round(t["seconds"] * 1000 * cpus[i] / tot)) for i in range(nchunks)},
                            "files": sorted(set(used))}
    for a, o in origs.items():
        setattr(mod, a, o)
    try: cls.doClassCleanups(); 
    except Exception: pass
json.dump(summary, sys.stderr)
