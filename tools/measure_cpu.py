"""CPU microseconds each test costs when the selector runs it in-process on the pinned
commit (one cold child per test class, cheapest test first, as the selector does).
Run inside the image:  python /app/tools/measure_cpu.py > cpu.json"""
import json, os, sys, time
sys.path.insert(0, "/app")
import selector as S

def usage():
    for l in open("/sys/fs/cgroup/cpu.stat"):
        if l.startswith("usage_usec"):
            return int(l.split()[1])

if __name__ != "__main__":
    raise SystemExit  # imported by a pool worker
tests = json.load(open(os.path.join(S.DATA, "tests.json")))["tests"]
pre = json.load(open(os.path.join(S.MAP, "preload.json")))
for m in ["unittest", "unittest.mock"] + pre["base"] + ["duckdb", "pandas", "numpy"]:
    try: __import__(m)
    except Exception: pass
groups = {}
for t in tests:
    f, c, m = t["id"].split("::")
    groups.setdefault((f, c), []).append(t)
out = {}
for (f, c), ts in sorted(groups.items()):
    ts.sort(key=lambda t: (t["seconds"], t["number"]))
    order = [tuple(t["id"].split("::")) + (None, None) for t in ts]
    last = [usage()]
    def cb(i, bad):
        u = usage(); out[ts[i]["id"]] = [u - last[0], int(bad)]; last[0] = u
        return True
    S.LABEL = True
    done, _ = S.run_tests("cpu", {}, order, [round(t["seconds"] * 1000) for t in ts], cb, time.monotonic() + 3600)
    print(f, c, len(ts), done, file=sys.stderr)
json.dump(out, sys.stdout, separators=(",", ":"))
