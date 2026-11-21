"""Run one test with its loop restricted to some units (same thing the selector does).
usage: unit_run.py <tests/file.py::Class::method> <module attribute> <unit,unit,...>
The attribute is an Enum the test iterates over; units are member names."""
import sys, unittest, importlib

file, cls, meth = sys.argv[1].split("::")
mod = importlib.import_module(file[:-3].replace("/", "."))
enum = getattr(mod, sys.argv[2])
setattr(mod, sys.argv[2], [enum[n] for n in sys.argv[3].split(",")])
r = unittest.TestResult()
unittest.TestSuite([getattr(mod, cls)(meth)]).run(r)
ok = r.wasSuccessful() and not r.unexpectedSuccesses
print("ok" if ok else "FAILED", file=sys.stderr)
sys.exit(0 if ok else 1)
