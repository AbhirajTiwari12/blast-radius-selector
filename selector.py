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
MAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map")
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
            continue
        if not cur["lines"]:
            cur["meta"].append(raw)
            continue
        if raw.startswith("-"):
            cur["lines"].append(("-", old, raw[1:]))
            old += 1
        elif raw.startswith("+"):
            cur["lines"].append(("+", old, raw[1:]))  # inserted before old line `old`
        elif raw.startswith(" ") or raw == "":
            cur["lines"].append((" ", old, raw[1:]))
            old += 1
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


def main():
    with open(os.path.join(DATA, "tests.json")) as f:
        tests = json.load(f)["tests"]
    number = {t["id"]: t["number"] for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    with open(os.path.join(MAP, "ids.json")) as f:
        ids = json.load(f)
    with open(os.path.join(MAP, "fixtures.json")) as f:
        fixtures = json.load(f)
    all_idx = range(len(ids))
    cdir = os.path.join(DATA, "commits")
    out = []
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".patch"):
            continue
        try:
            with open(os.path.join(cdir, name), encoding="utf-8", errors="replace") as f:
                sel = select(f.read(), fixtures, all_idx)
            nums = [number[ids[i]] for i in sel if ids[i] in number]
        except Exception as e:  # never lose a commit: fall back to running everything
            print(f"{name}: {e!r}", file=sys.stderr)
            nums = list(ms)
        out.append(json.dumps({"commit": name[:-6], "workers": split(nums, ms)}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
