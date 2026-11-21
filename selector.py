"""Blast Radius selector.

For every patch, map the changed lines to the tests that execute them, using maps built once
from the pinned sqlglot commit (per-test line coverage, and the data files each test opens).
Nothing here knows any patch ahead of time: the answer is computed from the diff's line
numbers and the coverage maps in map/.
"""
import json
import os
import re
import sys

DATA = os.environ.get("DATA_DIR", "/data")
MAP = os.environ.get("BLAST_MAP") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "map")
WORKER_MS = 2000
MAX_WORKERS = 8
CONFIG_FILES = {"pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", "tox.ini", "conftest.py"}

_files = {}


def load_file(rel):
    if rel not in _files:
        p = os.path.join(MAP, "files", rel.replace("/", "__") + ".json")
        try:
            with open(p) as f:
                _files[rel] = json.load(f)
        except OSError:
            _files[rel] = None
    return _files[rel]


def parse_patch(text):
    """Yield (old_path, new_path, hunks); a hunk is a list of (tag, old_lineno, text)."""
    files = []
    cur = None
    old = 0
    for raw in text.split("\n"):
        if raw.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", raw)
            cur = {"old": m.group(1) if m else None, "new": m.group(2) if m else None, "lines": [], "meta": []}
            files.append(cur)
            continue
        if raw.startswith("--- ") and (cur is None or not cur["lines"]):
            if cur is None or cur.get("seen_old"):
                cur = {"old": None, "new": None, "lines": [], "meta": []}
                files.append(cur)
            cur["seen_old"] = True
            p = raw[4:].split("\t")[0].strip()
            cur["old"] = None if p == "/dev/null" else (p[2:] if p.startswith(("a/", "b/")) else p)
            continue
        if cur is None:
            continue
        if raw.startswith("+++ ") and not cur["lines"]:
            p = raw[4:].split("\t")[0].strip()
            cur["new"] = None if p == "/dev/null" else (p[2:] if p.startswith(("a/", "b/")) else p)
            cur["seen_new"] = True
            continue
        if raw.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw)
            if m:
                old = int(m.group(1))
                if m.group(2) == "0":
                    old += 1  # "-N,0": insertion after line N
                cur["lines"].append(("@", old, ""))
                cur["left"] = [int(m.group(2) or 1), int(m.group(4) or 1)]
            continue
        if not cur["lines"]:
            cur["meta"].append(raw)
            continue
        left = cur.get("left")
        if left is not None and left[0] <= 0 and left[1] <= 0:
            continue  # past the end of the hunk (trailing blank line, mail signature, ...)
        if raw.startswith("-"):
            cur["lines"].append(("-", old, raw[1:]))
            old += 1
            left[0] -= 1
        elif raw.startswith("+"):
            cur["lines"].append(("+", old, raw[1:]))  # inserted before old line `old`
            left[1] -= 1
        elif raw.startswith(" ") or raw == "":
            cur["lines"].append((" ", old, raw[1:]))
            old += 1
            left[0] -= 1
            left[1] -= 1
        # "\ No newline at end of file" and anything else: ignored
    return files


def py_tests(rel, lines, selected):
    """Tests that can observe the edit described by `lines` in python file `rel`."""
    m = load_file(rel)
    if m is None:
        return  # no test imports this file
    line, sets, strl = m["line"], m["sets"], set(m.get("instr", ()))
    n = len(line)

    def at(ln):
        if 0 < ln < n and line[ln] >= 0:
            selected.update(sets[line[ln]])
            return True
        return False

    def neighbours(ln):
        # insertion before old line ln: nearest code line on each side
        k = ln - 1
        while k > 0 and not at(k):
            k -= 1
        k = ln
        while k < n and not at(k):
            k += 1

    i = 0
    while i < len(lines):
        tag = lines[i][0]
        if tag not in "+-":
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j][0] in "+-":
            j += 1
        block = lines[i:j]
        removed = [b for b in block if b[0] == "-"]
        added = [b for b in block if b[0] == "+"]
        hit = False
        for _, ln, _t in removed:
            hit = at(ln) or hit
        code_added = [b for b in added if b[2].strip() and not b[2].lstrip().startswith("#")]
        comment_added = [b for b in added if b[2].lstrip().startswith("#")]
        if code_added and not hit:
            neighbours(code_added[0][1])
        for _, ln, _t in comment_added:
            if ln in strl:  # lands inside a multi-line string: not a comment at all
                at(ln)
        if removed and not hit and not code_added:
            # removed only blank/comment lines: harmless unless inside a string
            pass
        i = j


def select(patch_text, fixtures, all_tests):
    selected = set()
    for f in parse_patch(patch_text):
        old, new = f["old"], f["new"]
        rel = old or new
        if rel is None:
            continue
        base = rel.rsplit("/", 1)[-1]
        renamed = old and new and old != new
        if rel.endswith(".py") and (rel.startswith("sqlglot/") or rel.startswith("tests/") or base in CONFIG_FILES):
            m = load_file(rel)
            if base == "conftest.py" or (m is None and base in CONFIG_FILES):
                return set(all_tests)
            if new is None or renamed or old is None:
                # deleted / moved / new module: everyone importing it (if anyone)
                if m:
                    selected.update(m["all"])
                continue
            py_tests(rel, f["lines"], selected)
        elif base in CONFIG_FILES:
            return set(all_tests)
        elif rel in fixtures:
            selected.update(fixtures[rel])
        elif old and new and renamed and old in fixtures:
            selected.update(fixtures[old])
    return selected


def split(tests, ms):
    """Choose the worker count and assignment that minimise busiest load + 2 s a worker."""
    if not tests:
        return []
    order = sorted(tests, key=lambda t: -ms[t])
    best = None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        loads = [0] * k
        workers = [[] for _ in range(k)]
        for t in order:
            w = loads.index(min(loads))
            loads[w] += ms[t]
            workers[w].append(t)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, workers)
        if max(loads) == ms[order[0]]:
            break  # the longest test alone is the bottleneck: more workers only cost
    return best[1]


# ---------------------------------------------------------------------------------------
# Run-to-decide: execute the covering tests against the patched tree and keep only those
# that do not pass. Everything below derives its answer from actually running sqlglot's
# own tests on the patched code; whenever a run is not possible or not conclusive the
# static selection above is used unchanged.

SRC = os.environ.get("BLAST_SRC", "/app/src")
# Cost model for running a test here, in CPU milliseconds, from its recorded seconds.
RHO = float(os.environ.get("BLAST_RHO", "1.5"))  # CPU per recorded second, cold process
POOL_RHO = 2.5  # tests that fan out to a process pool were recorded with several workers
RUN_OVERHEAD_MS = 400  # fork + import of sqlglot and one test module
HEAVY_IMPORT_MS = 600  # test modules that import duckdb/pandas
POOL_MS = 300  # pickling work to and from the pool
FIXTURE_MS = {"tests/test_executor.py::TestExecutor": 2500}  # setUpClass loading the TPC tables
KEEP = 0.8  # run only when the estimated CPU is below this share of what a clean run saves
BUDGET_S = float(os.environ.get("BLAST_BUDGET", "110"))  # wall seconds for all runs together
# Tests too expensive to ever run here (recorded seconds): kept whenever the map selects them,
# except under the evidence rule below.
EXPENSIVE_MS = 3600
LOOP_MAX_MS = 6000  # a unit-restricted run may cost up to this much (recorded-ms scale)
# Evidence rule: these tests replay whole TPC query sets through the optimizer, planner and
# executor. When every other covering test was run on the patched code and passed, they are
# left out. Measured on generated mutants (tools/README.md): see README for the numbers.
GATED = (
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
)
GATE_MIN_TESTS = 5  # need at least this many other covering tests as evidence
LABEL_EXPENSIVE = ()
VERBOSE = os.environ.get("BLAST_VERBOSE") == "1"
NO_RUN = os.environ.get("BLAST_NORUN") == "1"
GATE = os.environ.get("BLAST_NOGATE") != "1"
INLINE_POOL = os.environ.get("BLAST_REALPOOL") != "1"
FULL = os.environ.get("BLAST_FULL") == "1"  # validation: run every covering test to the end
LABEL = os.environ.get("BLAST_LABEL") == "1"  # validation: also the expensive tests, last
LABEL_ALL = os.environ.get("BLAST_LABEL_ALL") == "1"  # validation: never stop early
LABEL_MAX_MS = int(os.environ.get("BLAST_LABEL_MAX_MS", "12000"))


def static_cost(tests, ms):
    w = split(tests, ms)
    return (max(sum(ms[t] for t in x) for x in w) + WORKER_MS * len(w)) if w else 0


def apply_patch(f, old_text):
    """New text of a modified file, or None when the hunks do not fit the pinned source."""
    old = old_text.split("\n")
    out = []
    pos = 0  # index into old
    for tag, ln, text in f["lines"]:
        if tag == "@":
            if ln - 1 < pos:
                return None
            out.extend(old[pos:ln - 1])
            pos = ln - 1
        elif tag == "+":
            out.append(text)
        else:
            if pos >= len(old) or old[pos] != text:
                return None
            if tag == " ":
                out.append(text)
            pos += 1
    out.extend(old[pos:])
    return "\n".join(out)


def patched_files(patch_text):
    """{relpath: new text} when every file of the patch is a plain in-place edit of a file
    under sqlglot/ or tests/ that applies exactly; otherwise None (no run, static answer)."""
    if "\n\\ No newline" in patch_text or "GIT binary patch" in patch_text or "\r" in patch_text:
        return None
    res = {}
    for f in parse_patch(patch_text):
        old, new = f["old"], f["new"]
        if not old or old != new or old in res or not f["lines"]:
            return None
        if not old.startswith(("sqlglot/", "tests/")) or ".." in old:
            return None
        if any(m and not m.startswith("index ") for m in f["meta"]):
            return None
        p = os.path.join(SRC, old)
        if os.path.islink(p) or not os.path.isfile(p):
            return None
        try:
            with open(p, encoding="utf-8", newline="") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            return None
        if "\r" in text:
            return None
        newtext = apply_patch(f, text)
        if newtext is None or newtext == text:
            return None
        res[old] = newtext
    return res or None


def make_overlay(ov, files):
    """A copy of the checkout made of symlinks, with the patched files as real files."""
    real = {""}
    for rel in files:
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            real.add("/".join(parts[:i]))
    for d in sorted(real, key=len):
        os.makedirs(os.path.join(ov, d))
        for e in os.listdir(os.path.join(SRC, d)):
            p = d + "/" + e if d else e
            if p not in real and p not in files:
                os.symlink(os.path.join(SRC, p), os.path.join(ov, p))
    for rel, text in files.items():
        with open(os.path.join(ov, rel), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


def child_main(wfd, ov, order, probe):
    """In the forked child: import the patched tree and run the tests, one status line each."""
    import importlib
    import unittest
    import warnings

    os.setsid()
    try:  # one core: a process pool created by a test then starts a single worker
        os.sched_setaffinity(0, {int(os.environ.get("BLAST_CPU", min(os.sched_getaffinity(0))))})
    except (AttributeError, OSError):
        pass
    dn = os.open(os.devnull, os.O_RDWR)
    for fd in ((0, 1) if os.environ.get("BLAST_DEBUG") else (0, 1, 2)):
        os.dup2(dn, fd)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTHONPATH"] = ov
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    os.chdir(ov)
    sys.path.insert(0, ov)

    def say(s):
        os.write(wfd, (s + "\n").encode())

    if INLINE_POOL:
        # Tests that fan work out to a ProcessPoolExecutor get the same work done in this
        # process: same function, same pickling of arguments and results, no worker start-up.
        import concurrent.futures as cf
        import concurrent.futures.process as cfp
        import pickle

        class InlinePool:
            def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
                self._init = (initializer, initargs)
                self._started = False

            def _start(self):
                if not self._started:
                    self._started = True
                    initializer, initargs = self._init
                    if initializer is not None:
                        initializer(*pickle.loads(pickle.dumps(initargs)))

            def submit(self, fn, /, *args, **kwargs):
                f = cf.Future()
                try:
                    self._start()
                    fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
                    f.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
                except Exception as e:
                    f.set_exception(e)
                return f

            def map(self, fn, *iterables, timeout=None, chunksize=1):
                fs = [self.submit(fn, *a) for a in zip(*iterables)]
                return (f.result() for f in fs)

            def shutdown(self, wait=True, *, cancel_futures=False):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        cf.ProcessPoolExecutor = cfp.ProcessPoolExecutor = InlinePool

    if probe:
        try:
            for name in probe:
                importlib.import_module(name)
        except BaseException:
            say("P")
            os._exit(0)
        say("Q")
    cases = []
    for i, (file, cls, meth, attr, units) in enumerate(order):
        try:
            mod = importlib.import_module(file[:-3].replace("/", "."))
            case = getattr(mod, cls)(meth)
            if attr:  # the test loops over units: restrict the loop to some of them
                case._blast_loop = (attr, units)
        except BaseException:
            if os.environ.get("BLAST_DEBUG"):
                import traceback
                traceback.print_exc()
            say("X")
            os._exit(0)
        case._blast_i = i
        cases.append(case)

    class Result(unittest.TestResult):
        def startTest(self, test):
            super().startTest(test)
            self._bad = False
            loop = getattr(test, "_blast_loop", None)
            if loop:
                (where, attr), units = loop
                if where == "module":  # an Enum in the test's module, units are member names
                    holder = sys.modules[type(test).__module__]
                    full = getattr(holder, attr)
                    part = [full[u] for u in units]
                else:  # a list on the test class (made by its fixture), units are positions
                    holder = type(test)
                    full = getattr(holder, attr)
                    part = [full[int(u)] for u in units]
                self._loop = (holder, attr, full)
                setattr(holder, attr, part)
            # what pytest does around every test
            self._cw = warnings.catch_warnings(record=True)
            self._cw.__enter__()
            warnings.filterwarnings("always", category=DeprecationWarning)
            warnings.filterwarnings("always", category=PendingDeprecationWarning)

        def stopTest(self, test):
            if getattr(test, "_blast_loop", None) and getattr(self, "_loop", None):
                setattr(*self._loop)
                self._loop = None
            self._cw.__exit__(None, None, None)
            super().stopTest(test)
            say("%d %d" % (test._blast_i, 1 if self._bad else 0))

        def _mark(self, test):
            if hasattr(test, "_blast_i"):
                self._bad = True
            else:  # class or module fixture failed
                say("X")
                os._exit(0)

        def addError(self, test, err):
            self._mark(test)

        def addFailure(self, test, err):
            self._mark(test)

        def addUnexpectedSuccess(self, test):
            self._mark(test)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self._mark(test)

    try:
        unittest.TestSuite(cases).run(Result())
        say("D")
    except BaseException:
        say("X")
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(0)


def run_tests(cid, files, order, expect_ms, on_result, deadline, probe=()):
    """Fork, import the modules in `probe`, then run `order`, all on the patched tree. Calls
    on_result(i, failed) per finished test; it returns False to stop. Returns (ran everything
    asked, the probe modules imported)."""
    import select
    import shutil
    import signal
    import time

    ov = "/tmp/blast-ov/" + cid
    shutil.rmtree(ov, ignore_errors=True)
    make_overlay(ov, files)
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            child_main(w, ov, order, probe)
        finally:
            os._exit(1)
    os.close(w)
    buf = b""
    nxt = 0
    done = False
    alive = True
    probed = not probe
    try:
        while alive:
            allow = max(12.0, 15 * expect_ms[nxt] / 1000) if nxt < len(order) else 12.0
            if LABEL:
                allow = max(240.0, allow * 4)
            allow = min(allow, max(0.0, deadline - time.monotonic()))
            ready = select.select([r], [], [], allow)[0]
            if not ready:
                break  # too slow or out of budget
            chunk = os.read(r, 65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line == b"D":
                    done = nxt == len(order)
                    alive = False
                    break
                if line == b"Q":
                    probed = True
                    continue
                if line in (b"X", b"P"):
                    alive = False
                    break
                i, bad = line.split()
                if int(i) != nxt:
                    alive = False
                    break
                nxt += 1
                if not on_result(int(i), bad == b"1"):
                    alive = False
                    break
    finally:
        os.close(r)
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        # this process may be PID 1: collect orphaned grandchildren (pool workers) too
        waits = 0
        while waits < 10:
            try:
                got = os.waitpid(-1, os.WNOHANG)[0]
            except OSError:
                break  # no children left
            if not got:
                waits += 1
                time.sleep(0.005)
        shutil.rmtree(ov, ignore_errors=True)
    return done, probed


def est_cpu_ms(run, ms, ctx):
    """Estimated CPU of running `run` in one child."""
    cost = RUN_OVERHEAD_MS
    pool, tests, heavy = ctx["pool"], ctx["tests"], ctx["preload"]["heavy"]
    ms = ctx["run_ms"]
    classes = set()
    pooled = False
    for t in run:
        if t in pool:
            cost += ms[t] * POOL_RHO
            pooled = True
        else:
            cost += ms[t] * RHO
        classes.add(tests[t]["id"].rsplit("::", 1)[0])
    for c in classes:
        cost += FIXTURE_MS.get(c, 0) + (HEAVY_IMPORT_MS if c.split("::")[0] in heavy else 0)
    return cost + (POOL_MS if pooled else 0)


def cpu_now_ms():
    """CPU used so far by this container (cgroup), else by this process and its children."""
    try:
        with open("/sys/fs/cgroup/cpu.stat") as f:
            for line in f:
                if line.startswith("usage_usec"):
                    return int(line.split()[1]) / 1000
    except (OSError, ValueError):
        pass
    return sum(os.times()[:4]) * 1000


def cpu_scale(ctx):
    """Measured CPU of the runs so far over their estimates: the cost model corrects itself
    to the machine it runs on (starts at 1 with the weight of a 4 s run)."""
    cal = ctx["cal"]
    return min(2.5, max(0.4, cal["act"] / cal["est"]))


def plan_run(sel, ms, ctx):
    """(tests to run, gate). Cheapest-first prefix of the covering tests worth running: the
    CPU it costs must be clearly less than what dropping those tests saves if they all pass.
    gate=True: the run is every non-expensive covering test, and if all of them pass the
    gated expensive tests are dropped too."""
    never, gated, run_ms = ctx["never"], ctx["gated"], ctx["run_ms"]
    # unit-restricted tests first (they free the most for what they cost), then cheapest first
    order = sorted((t for t in sel if t not in never), key=lambda t: (run_ms[t] == ms[t], run_ms[t], t))
    kept = [t for t in sel if t in never]
    if LABEL:
        if sum(ms[t] for t in order) > LABEL_MAX_MS:
            return [], False
        if LABEL_ALL:
            if os.environ.get("BLAST_LABEL_NOEXP") == "1":
                return order, False
            return order + sorted((t for t in kept if t in gated), key=lambda t: ms[t]), False
        return order + sorted((t for t in kept if ms[t] < 25000), key=lambda t: ms[t]), False
    if FULL:
        return order, False
    base = static_cost(sel, ms)
    scale = cpu_scale(ctx)
    best, best_gain, best_gate = 0, 0, False
    step = max(1, len(order) // 40)
    for k in range(1, len(order) + 1):
        if k < len(order) and run_ms[order[k - 1]] == ms[order[k - 1]] and (
            run_ms[order[k]] == run_ms[order[k - 1]] or k % step
        ):
            continue  # (prefixes ending in a unit-restricted test are always considered)
        gain = KEEP * (base - static_cost(order[k:] + kept, ms)) - scale * est_cpu_ms(order[:k], ms, ctx)
        if gain > best_gain:
            best, best_gain = k, gain
    # the evidence rule needs every covering test below the expensive ones to be run
    complete = all(ms[t] >= EXPENSIVE_MS for t in kept)
    if GATE and complete and len(order) >= GATE_MIN_TESTS and any(t in gated for t in kept):
        left = [t for t in kept if t not in gated]
        gain = KEEP * (base - static_cost(left, ms)) - scale * est_cpu_ms(order, ms, ctx)
        if gain > best_gain:
            best, best_gain, best_gate = len(order), gain, True
    return order[:best], best_gate


def import_time_change(rel, lines):
    """Does the edit touch a statement that runs when the module is imported (module or
    class level, or a def header)? Then the module may stop importing at all."""
    m = load_file(rel)
    if m is None:
        return True
    imp = m.get("imp_set")
    if imp is None:
        imp = m["imp_set"] = set(m.get("imp", ()))
    line = m["line"]
    n = len(line)
    for tag, ln, text in lines:
        if tag == "-":
            if ln in imp:
                return True
        elif tag == "+" and text.strip() and not text.lstrip().startswith("#"):
            k = ln - 1
            while k > 0 and line[k] < 0:
                k -= 1
            if k in imp:
                return True
            k = ln
            while k < n and line[k] < 0:
                k += 1
            if k in imp:
                return True
    return False


def changed_blocks(old_text, new_text):
    """The top-level block(s) around the differing region, as (old source, new source)."""
    old, new = old_text.split("\n"), new_text.split("\n")
    a = 0
    while a < len(old) and a < len(new) and old[a] == new[a]:
        a += 1
    b = 0
    while b < len(old) - a and b < len(new) - a and old[-1 - b] == new[-1 - b]:
        b += 1
    end = len(new) - b  # changed region of new is [a, end)
    heads = ("def ", "class ", "async def ", "@")
    start = min(a, len(new) - 1)
    while start > 0 and not new[start].startswith(heads):
        start -= 1
    while start > 0 and new[start - 1].startswith("@"):
        start -= 1
    if not new[start].startswith(heads):
        start = 0
    stop = max(end, a + 1)
    while stop < len(new) and not new[stop].startswith(heads):
        stop += 1
    return "\n".join(old[start:stop - (len(new) - len(old))]) + "\n", "\n".join(new[start:stop]) + "\n"


def same_code(x, y):
    """Two code objects equal in everything but line numbers."""
    if (
        x.co_code != y.co_code or x.co_names != y.co_names or x.co_varnames != y.co_varnames
        or x.co_freevars != y.co_freevars or x.co_cellvars != y.co_cellvars
        or x.co_flags != y.co_flags or x.co_argcount != y.co_argcount
        or x.co_kwonlyargcount != y.co_kwonlyargcount or x.co_posonlyargcount != y.co_posonlyargcount
        or x.co_name != y.co_name or x.co_qualname != y.co_qualname
        or x.co_exceptiontable != y.co_exceptiontable or len(x.co_consts) != len(y.co_consts)
    ):
        return False
    for u, v in zip(x.co_consts, y.co_consts):
        if hasattr(u, "co_code") or hasattr(v, "co_code"):
            if not (hasattr(u, "co_code") and hasattr(v, "co_code") and same_code(u, v)):
                return False
        elif type(u) is not type(v) or repr(u) != repr(v):
            return False
    return True


def compare_block(old_text, new_text, rel):
    """'broken' when the changed block no longer compiles, 'same' when it compiles to the
    same code as before (only layout or comments changed), else 'changed'."""
    try:
        ob, nb = changed_blocks(old_text, new_text)
        new_code = compile(nb, rel, "exec", dont_inherit=True)
    except Exception:
        return "broken"
    try:
        if same_code(compile(ob, rel, "exec", dont_inherit=True), new_code):
            return "same"
    except Exception:
        pass
    return "changed"


def heavy_ready(ctx):
    """Load the big libraries some test files import (duckdb, pandas, numpy) once, here, so
    that children inherit them. They are first imported in a throw-away child with a time
    limit: a native library that hangs or spawns threads must not take this process down."""
    import signal
    import time

    state = ctx["heavy_state"]
    if "ok" in state:
        return state["ok"]
    names = sorted({n for v in ctx["preload"]["heavy"].values() for n in v})
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            dn = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(dn, fd)
            for n in names:
                __import__(n)
            code = 0
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("Threads:") and int(line.split()[1]) > 1:
                        code = 3  # importing starts threads: do not fork from such a process
        except BaseException:
            pass
        finally:
            os._exit(code)
    end = time.monotonic() + 20
    status = None
    while time.monotonic() < end:
        got, st = os.waitpid(pid, os.WNOHANG)
        if got:
            status = st
            break
        time.sleep(0.02)
    if status is None:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except OSError:
            pass
        state["ok"] = False
        return False
    code = os.waitstatus_to_exitcode(status)
    if code == 0:
        for n in names:
            __import__(n)
    state["ok"] = code in (0, 3)  # 3: usable, but every child imports them itself
    return state["ok"]


def decide(cid, patch_text, sel, units_hit, ms, ctx, deadline):
    """Tests that still have to be run by the workers, starting from the static selection."""
    import time

    files = patched_files(patch_text)
    if files is None:
        return sel
    probe = []
    unchanged = 0
    parsed = {f["old"]: f for f in parse_patch(patch_text)}
    extra = set()
    for rel, newtext in files.items():
        if not rel.endswith(".py"):
            continue
        with open(os.path.join(SRC, rel), encoding="utf-8", newline="") as fh:
            oldtext = fh.read()
        verdict = compare_block(oldtext, newtext, rel)
        if verdict == "same":
            unchanged += 1
        elif verdict == "broken":
            extra.update(ctx["importers"](rel))  # the file no longer compiles
        elif import_time_change(rel, parsed[rel]["lines"]):
            name = rel[:-3].replace("/", ".")
            probe.append(name[:-9] if name.endswith(".__init__") else name)
    if extra:
        return sorted(set(sel) | extra)
    if unchanged == len(files):
        return []  # every edited file compiles to exactly the code it had
    if NO_RUN or time.monotonic() > deadline:
        return sel
    test_by_num, preload = ctx["tests"], ctx["preload"]
    # A test that loops over units (its subtests) and is hit only through some of them is run
    # on those units alone: its cost here is that share of its recorded time.
    ctx = dict(ctx)
    ctx["run_ms"] = run_ms = dict(ms)
    loops = {}
    for t, hit in units_hit.items():
        spec = ctx["units"].get(t)
        if spec and hit and t in sel and set(hit) <= set(spec["units"]):
            if "ms" in spec:  # measured cost of each unit
                share = sum(spec["ms"].get(u, 0) for u in hit)
            else:
                share = round(ms[t] * len(hit) / len(spec["units"]))
            if share < LOOP_MAX_MS:
                run_ms[t] = share
                loops[t] = ((spec.get("on", "module"), spec["attr"]), sorted(hit))
    ctx["never"] = never = {t for t in ctx["never"] if t not in loops}
    if any(test_by_num[t]["file"] in preload["heavy"] for t in sel) and not heavy_ready(ctx):
        # duckdb/pandas cannot be loaded here: leave the tests that need them to the workers
        ctx["never"] = never = never | {t for t in sel if test_by_num[t]["file"] in preload["heavy"]}
    run, gate = [], False
    if sel and all(rel.endswith(".py") for rel in files):
        # (data files are read by few tests: the static answer is already small)
        run, gate = plan_run(sel, ms, ctx)
    if not run and not probe:
        return sel
    # group by class so that class fixtures run once, cheapest class first
    groups = {}
    for t in run:
        f, c, m = test_by_num[t]["id"].split("::")
        groups.setdefault((f, c), []).append(t)
    run = [t for g in sorted(groups.values(), key=lambda g: (sum(run_ms[x] for x in g), g)) for t in g]
    if LABEL:
        run = [t for t in run if t not in never] + sorted((t for t in run if t in never), key=lambda t: ms[t])
    # unit-restricted tests go first (they settle the most expensive tests), followed by the
    # rest of their own class so that its fixture is built once
    first = [t for t in run if t in loops]
    first_cls = {test_by_num[t]["id"].rsplit("::", 1)[0] for t in first}
    same = [t for t in run if t not in loops and test_by_num[t]["id"].rsplit("::", 1)[0] in first_cls]
    run = first + same + [t for t in run if t not in loops and t not in set(same)]
    order = [tuple(test_by_num[t]["id"].split("::")) + loops.get(t, (None, None)) for t in run]
    passed, failed = set(), set()
    state = {"rest": sum(run_ms[t] for t in run)}
    run_set = set(run)

    def on_result(i, bad):
        t = run[i]
        state["rest"] -= run_ms[t]
        (failed if bad else passed).add(t)
        if LABEL:  # stop at the first cheap failure; run every expensive test once reached
            return LABEL_ALL or t in never or not failed
        if FULL or not failed:
            return True
        # something fails: go on only while finishing costs less than it can still save
        remaining = [x for x in sel if x not in passed]
        floor = [x for x in remaining if x in failed or x not in run_set]
        return state["rest"] * RHO < 0.5 * (static_cost(remaining, ms) - static_cost(floor, ms))

    t1 = time.monotonic()
    c1 = os.times()
    u1 = cpu_now_ms()
    done, probed = run_tests(cid, files, order, [run_ms[t] for t in run], on_result, deadline, probe)
    if done and run and len(passed) + len(failed) == len(run):
        ctx["cal"]["act"] += cpu_now_ms() - u1
        ctx["cal"]["est"] += est_cpu_ms(run, ms, ctx)
    if LABEL:
        print("LABEL " + json.dumps({"c": cid, "sel": sel, "run": run, "passed": sorted(passed), "failed": sorted(failed), "done": done, "probed": probed}), file=sys.stderr)
    if VERBOSE:
        c2 = os.times()
        print("%s: cover %d (%.1fs) run %d (%.2fs rec, est %.1fs) gate %s passed %d failed %d done %s probe %s/%s wall %.2f cpu %.2f" % (
            cid, len(sel), static_cost(sel, ms) / 1000, len(run), sum(run_ms[t] for t in run) / 1000, est_cpu_ms(run, ms, ctx) / 1000,
            "%s scale %.2f loops %s" % (gate, cpu_scale(ctx), sorted(len(v[1]) for v in loops.values())),
            len(passed), len(failed), done, probe, probed, time.monotonic() - t1, c2[2] + c2[3] - c1[2] - c1[3]), file=sys.stderr)
    if not probed:
        # a patched module does not import (or hangs): every test that loads it fails
        for rel in files:
            extra.update(ctx["importers"](rel))
        return sorted(set(sel) | extra)
    if gate and done and not failed and len(passed) == len(run):
        # every other covering test ran on the patched code and passed
        return [t for t in sel if t not in passed and t not in ctx["gated"]]
    return [t for t in sel if t not in passed]


def main():
    import time

    t0 = time.monotonic()
    with open(os.path.join(DATA, "tests.json")) as f:
        tests = json.load(f)["tests"]
    number = {t["id"]: t["number"] for t in tests}
    test_by_num = {t["number"]: t for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    with open(os.path.join(MAP, "ids.json")) as f:
        ids = json.load(f)
    with open(os.path.join(MAP, "fixtures.json")) as f:
        fixtures = json.load(f)
    all_idx = range(len(ids))
    cdir = os.path.join(DATA, "commits")
    names = sorted(n for n in os.listdir(cdir) if n.endswith(".patch"))
    static, texts, units_hit = {}, {}, {}
    all_idx = [i for i in all_idx if ids[i] in number]
    for name in names:
        try:
            with open(os.path.join(cdir, name), encoding="utf-8", errors="replace") as f:
                texts[name] = f.read()
            sel = select(texts[name], fixtures, all_idx)
            static[name] = sorted(number[ids[i]] for i in sel if ids[i] in number)
            for i in sel:  # units of a looping test: "<test id>[<unit>]"
                if ids[i] not in number and ids[i].endswith("]"):
                    parent, unit = ids[i][:-1].split("[", 1)
                    if parent in number:
                        units_hit.setdefault(name, {}).setdefault(number[parent], []).append(unit)
        except Exception as e:  # never lose a commit: fall back to running everything
            print(f"{name}: {e!r}", file=sys.stderr)
            static[name] = sorted(ms)
            texts.pop(name, None)
    final = dict(static)
    ctx = None
    deadline = t0 + BUDGET_S
    have_src = os.path.isdir(os.path.join(SRC, "sqlglot"))
    # smallest covering sets first: the cost model is calibrated before the big decisions
    for name in sorted(names, key=lambda n: (sum(ms[t] for t in static[n]), n)):
        if name not in texts or not have_src:
            continue
        try:
            if ctx is None:
                with open(os.path.join(MAP, "preload.json")) as f:
                    preload = json.load(f)
                with open(os.path.join(MAP, "procs.json")) as f:
                    pool = {number[i] for i in json.load(f)["pool"] if i in number}
                with open(os.path.join(MAP, "units.json")) as f:
                    units = {number[k]: v for k, v in json.load(f).items() if k in number}

                def importers(rel):
                    m = load_file(rel)
                    return {number[ids[i]] for i in (m["all"] if m else ()) if ids[i] in number}

                ctx = {
                    "preload": preload, "pool": pool, "tests": test_by_num, "importers": importers,
                    "units": units, "run_ms": ms, "heavy_state": {}, "cal": {"act": 4000.0, "est": 4000.0},
                    "never": {t for t in ms if ms[t] >= EXPENSIVE_MS},
                    "gated": {number[i] for i in GATED if i in number},
                }
                for m in ["unittest", "unittest.mock", "importlib", "warnings", "select", "shutil", "signal"] + preload["base"]:
                    try:
                        __import__(m)
                    except Exception:
                        pass
            final[name] = decide(
                name[:-6], texts[name], static[name], units_hit.get(name, {}), ms, ctx, deadline
            )
        except Exception as e:
            print(f"{name}: run failed: {e!r}", file=sys.stderr)
            final[name] = static[name]
    out = [
        json.dumps({"commit": n[:-6], "workers": split(final[n], ms)}, separators=(",", ":"))
        for n in names
    ]
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
