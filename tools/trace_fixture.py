"""Trace of what importing a test module and running a test class's setUpClass executes
(pinned commit), and which tests of the class use what the fixture builds.

usage: trace_fixture.py OUT_DIR INDEX tests/file.py::Class   (cwd = sqlglot checkout)
Writes OUT_DIR/<INDEX>.json in the format of sitecustomize.py and prints one JSON line:
{"<class id>": [tests that read an attribute setUpClass assigns, directly or via a helper]}"""
import ast, importlib, json, os, sys

if __name__ != "__main__":
    raise SystemExit
out, index, target = sys.argv[1], int(sys.argv[2]), sys.argv[3]
file, cname = target.split("::")
root = os.getcwd() + "/"
sys.path.insert(0, root)
mon = sys.monitoring
TOOL = 3
mon.use_tool_id(TOOL, "blast-f")
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
mod = importlib.import_module(file[:-3].replace("/", "."))
cls = getattr(mod, cname)
cls.setUpClass()
mon.set_events(TOOL, 0)
n = len(root)
json.dump({"func": {k[n:]: sorted(v) for k, v in func.items()}, "imp": {k[n:]: sorted(v) for k, v in imp.items()},
           "classes": classes, "opened": []}, open("%s/%d.json" % (out, index), "w"), separators=(",", ":"))
# which tests use the fixture
tree = ast.parse(open(file).read())
node = [x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == cname][0]
def self_attrs(f):
    return {a.attr for a in ast.walk(f) if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id in ("self", "cls")}
funcs = {f.name: f for f in node.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
built = set()
for a in ast.walk(funcs["setUpClass"]):
    if isinstance(a, ast.Attribute) and isinstance(a.ctx, ast.Store) and isinstance(a.value, ast.Name) and a.value.id == "cls":
        built.add(a.attr)
uses = {k: self_attrs(f) for k, f in funcs.items()}
needy = {k for k, v in uses.items() if v & built}
changed = True
while changed:
    changed = False
    for k, v in uses.items():
        if k not in needy and v & needy:
            needy.add(k); changed = True
print(json.dumps({target: sorted(target + "::" + k for k in needy if k.startswith("test"))}))
