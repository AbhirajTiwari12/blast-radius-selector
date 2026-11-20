"""Build per-file line -> tests maps from per-test coverage of the pinned sqlglot commit.

Inputs (made by tools/run_tests.sh): OUT/<n>.json trace files, one per test run in
its own process (lines executed, data files under tests/ opened), tests.tsv.
Output: map/ids.json, map/files/<path>.json, map/fixtures.json
"""
import ast, json, os, sys, glob

OUT, SRC, DEST, TSV = sys.argv[1:5]
ids = [l.rstrip("\n").split("\t")[1] for l in open(TSV)]
cover = {}  # relpath -> {line: set(testidx)}: lines run inside functions
icover = {}  # relpath -> {line: set(testidx)}: lines run at import (module and class bodies)
inst = {}  # "module:Qual.Name" -> tests that instantiated that class or a subclass of it
opened = {}  # relpath -> set(testidx)
for n, _ in enumerate(ids):
    parts = glob.glob(f"{OUT}/{n}.json") + glob.glob(f"{OUT}/{n}.json.*")  # one per process
    if not parts:
        print("missing", n, file=sys.stderr); continue
    for p in parts:
        t = json.load(open(p))
        for kind, dest in (("func", cover), ("imp", icover)):
            for rel, lines in t[kind].items():
                d = dest.setdefault(rel, {})
                for ln in lines:
                    d.setdefault(ln, set()).add(n)
        for k, mro in t["classes"].items():
            inst.setdefault(k, set()).add(n)
            for b in mro:
                inst.setdefault(b, set()).add(n)
        for rel in t["opened"]:
            opened.setdefault(rel, set()).add(n)

def spans(tree, nlines):
    """For each line: (lo, hi) range of lines whose coverage decides who runs this line."""
    span = [None] * (nlines + 2)
    owner = [None] * (nlines + 2)   # innermost class (qualname, lo, hi) for class-body statements
    isdef = [False] * (nlines + 2)  # function signature / decorator lines
    def header_end(node):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            lo = min([first.lineno] + [d.lineno for d in getattr(first, "decorator_list", [])])
            return lo - 1
        return node.end_lineno
    def start(node):
        return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    def visit(node, infunc, cls):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                s, e = start(child), child.end_lineno
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # signature/decorators: whoever runs the body
                    for ln in range(s, header_end(child) + 1):
                        span[ln] = (s, e); isdef[ln] = True
                    visit(child, True, None)
                elif isinstance(child, ast.ClassDef):
                    he = header_end(child)
                    q = (cls[0] + "." if cls else "") + child.name
                    me = None if infunc else (q, s, e)
                    for ln in range(s, he + 1):
                        span[ln] = (s, he); owner[ln] = me
                    visit(child, infunc, me)
                elif hasattr(child, "body") and isinstance(child.body, list):
                    he = header_end(child)
                    for ln in range(s, he + 1):
                        span[ln] = (s, he); owner[ln] = cls
                    visit(child, infunc, cls)
                else:
                    for ln in range(s, e + 1):
                        span[ln] = (s, e); owner[ln] = cls
            else:
                visit(child, infunc, cls)
    visit(tree, False, None)
    return span, owner, isdef

def string_lines(src):
    """Lines L such that text inserted before L lands inside a multi-line string."""
    import io, tokenize
    res, fstart = set(), []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
                res.update(range(tok.start[0] + 1, tok.end[0] + 1))
            elif tok.type == getattr(tokenize, "FSTRING_START", -1):
                fstart.append(tok.start[0])
            elif tok.type == getattr(tokenize, "FSTRING_END", -1) and fstart:
                s0 = fstart.pop()
                if tok.end[0] > s0:
                    res.update(range(s0 + 1, tok.end[0] + 1))
    except (tokenize.TokenError, SyntaxError):
        pass
    return sorted(res)

os.makedirs(DEST + "/files", exist_ok=True)
json.dump(ids, open(DEST + "/ids.json", "w"))
index = {}
for rel in sorted(set(cover) | set(icover)):
    d, di = cover.get(rel, {}), icover.get(rel, {})
    path = os.path.join(SRC, rel)
    src = open(path).read()
    nlines = src.count("\n") + 1
    try:
        span, owner, isdef = spans(ast.parse(src), nlines)
    except SyntaxError:
        span = owner = [None] * (nlines + 2); isdef = [False] * (nlines + 2)
    module = rel[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[:-9]
    importers = set().union(*di.values()) if di else set()
    classcache = {}
    def class_users(o):
        # tests that built an instance of the class (or a subclass), or ran any of its code
        if o not in classcache:
            s = set(inst.get(module + ":" + o[0], ()))
            for k in range(o[1], o[2] + 1):
                s |= d.get(k, set())
            classcache[o] = s
        return classcache[o]
    sets, setidx, line = [], {}, [-1] * (nlines + 2)
    for ln in range(1, nlines + 1):
        sp = span[ln]
        if sp is None:
            continue  # blank/comment line: resolved from neighbours at run time
        s = set()
        at_import = False
        for k in range(sp[0], sp[1] + 1):
            s |= d.get(k, set())
            at_import = at_import or k in di
        if at_import and not isdef[ln]:
            # a module- or class-level statement: no test "runs" it, it shapes what is imported
            s |= class_users(owner[ln]) if owner[ln] and rel.startswith("sqlglot/") else importers
        key = tuple(sorted(s))
        if key not in setidx:
            setidx[key] = len(sets); sets.append(list(key))
        line[ln] = setidx[key]
    allt = sorted(importers | (set().union(*d.values()) if d else set()))
    out = DEST + "/files/" + rel.replace("/", "__") + ".json"
    json.dump({"sets": sets, "line": line, "all": allt, "instr": string_lines(src)}, open(out, "w"), separators=(",", ":"))
    index[rel] = len(allt)
json.dump({k: sorted(v) for k, v in sorted(opened.items())}, open(DEST + "/fixtures.json", "w"), separators=(",", ":"))
print(len(index), "python files,", len(opened), "data files", file=sys.stderr)
