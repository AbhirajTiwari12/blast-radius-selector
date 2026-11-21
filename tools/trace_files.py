"""Which tests list a directory or read a file of the checkout as data (pinned commit).

usage: trace_files.py TESTS_JSON > files.json   (cwd = sqlglot checkout, SKIP_INTEGRATION=0)
Runs the whole suite in this process and records, per test, every directory listing and
every file opened under the checkout that does not come from the import system. Reads done
while a test module is imported count for all tests of that module."""
import json, os, sys, unittest, importlib

if __name__ != "__main__":
    raise SystemExit  # imported by a pool worker
root = os.getcwd() + "/"
sys.path.insert(0, root)
cur = [None]
lists, opens = {}, {}

def from_import():
    f = sys._getframe(2)
    while f is not None:
        fn = f.f_code.co_filename
        if fn.startswith("<frozen importlib") or fn.endswith(("/linecache.py", "/tokenize.py", "/traceback.py", "/py_compile.py")):
            return True
        f = f.f_back
    return False

def hook(ev, args):
    if ev in ("os.listdir", "os.scandir"):
        p = args[0] if args else None
        dest = lists
    elif ev == "open":
        p = args[0]
        dest = opens
    else:
        return
    if not isinstance(p, str):
        try: p = os.fsdecode(p)
        except Exception: return
    p = os.path.abspath(p) if p else root
    if not (p + "/").startswith(root) or "__pycache__" in p or p.endswith(".pyc"):
        return
    if cur[0] is None or from_import():
        return
    dest.setdefault(p[len(root):] if len(p) >= len(root) else "", set()).add(cur[0])

sys.addaudithook(hook)
tests = json.load(open(sys.argv[1]))["tests"]
groups = {}
for t in tests:
    f, c, m = t["id"].split("::")
    groups.setdefault(f, {}).setdefault(c, []).append(m)

class R(unittest.TestResult):
    def startTest(self, test):
        cur[0] = self.prefix + type(test).__name__ + "::" + test._testMethodName
    def stopTest(self, test):
        cur[0] = self.cls_marker

devnull = open(os.devnull, "w")
for f, classes in sorted(groups.items()):
    cur[0] = "import:" + f
    mod = importlib.import_module(f[:-3].replace("/", "."))
    for c, meths in sorted(classes.items()):
        r = R(); r.prefix = f + "::"; r.cls_marker = "class:" + f + "::" + c
        cur[0] = r.cls_marker
        so, se = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = devnull
        try:
            unittest.TestSuite([getattr(mod, c)(m) for m in sorted(meths)]).run(r)
        finally:
            sys.stdout, sys.stderr = so, se
        print(f, c, len(meths), len(r.errors) + len(r.failures), file=sys.stderr)
    cur[0] = None
json.dump({"lists": {k: sorted(v) for k, v in sorted(lists.items())}, "opens": {k: sorted(v) for k, v in sorted(opens.items())}}, sys.stdout)
