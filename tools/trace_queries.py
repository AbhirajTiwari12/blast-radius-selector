"""Per-query traces of TestExecutor.test_execute_tpcds / test_execute_tpch on the pinned commit.

usage: trace_queries.py OUT_DIR FIRST_INDEX   (cwd = sqlglot checkout, SKIP_INTEGRATION=0)
Writes OUT_DIR/<n>.json for each query (n counts up from FIRST_INDEX, tpcds first) in the
format of sitecustomize.py, and prints "<n>\t<test id>[<i>]\t<cpu ms>" lines. A query's trace
is everything the test does for that query (parse, pickle round trip as the process pool
does, execute, compare) plus what import and class set-up ran, which every query shares."""
import json, os, pickle, sys, time

out, first = sys.argv[1], int(sys.argv[2])
root = os.getcwd() + "/"
sys.path.insert(0, root)
mon = sys.monitoring
TOOL = 3
mon.use_tool_id(TOOL, "blast-q")
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
    func.clear(); imp.clear(); classes.clear()
    mon.restart_events()

import tests.test_executor as te
from sqlglot import parse_one, find_tables
from sqlglot.executor import execute
cls = te.TestExecutor
cls.setUpClass()
pre = snap()
n = first
for test, sqls, schema, tables, tpch in (
    ("test_execute_tpcds", cls.tpcds_sqls, te.TPCDS_SCHEMA, cls.tpcds_tables, False),
    ("test_execute_tpch", cls.tpch_sqls, te.TPCH_SCHEMA, cls.tpch_tables, True),
):
    case = cls(test)
    for i, (meta, sql, _) in enumerate(sqls):
        reset()
        t = time.process_time()
        expression = pickle.loads(pickle.dumps(parse_one(sql)))
        if meta.get("execute"):
            tb = {x.name: tables[x.name] for x in find_tables(expression)}
            table = pickle.loads(pickle.dumps(execute(expression, schema=schema, tables=tb)))
            case.subtestHelper(i, table, tpch=tpch)
        cpu = time.process_time() - t
        f, im, cl = snap()
        for k, v in pre[0].items(): f.setdefault(k, set()).update(v)
        for k, v in pre[1].items(): im.setdefault(k, set()).update(v)
        cl.update(pre[2])
        json.dump({"func": {k: sorted(v) for k, v in f.items()}, "imp": {k: sorted(v) for k, v in im.items()},
                   "classes": cl, "opened": []}, open("%s/%d.json" % (out, n), "w"), separators=(",", ":"))
        print("%d\ttests/test_executor.py::TestExecutor::%s[%d]\t%d\t%d" % (n, test, i, cpu * 1000, 1 if meta.get("execute") else 0), flush=True)
        n += 1
