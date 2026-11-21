# Blast Radius selector

Picks, for each patch against sqlglot at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, the tests
that can observe the change, and splits them across workers.

Nothing here knows any patch, or any kind of edit, ahead of time. An answer comes from three
things only: the diff, maps built from the pinned commit, and actually running sqlglot's own
tests on the patched code while deciding.

## 1. Static selection (maps of the pinned commit)

`map/` is built once from the pinned commit only (`tools/`):

- every test is run alone in its own process and the lines it executes are recorded, so each
  source line maps to the tests that run it; module-level lines map to the tests that import
  the module, class-level lines to the tests that use the class;
- the non-Python files under `tests/` that each test opens are recorded, so a fixture maps
  to the tests that read it;
- which tests list a directory of the checkout or read one of its python files as text
  (`dirs.json`): a file added to or removed from such a directory, or any edit of such a
  file, selects them;
- which tests hand work to a process pool (`procs.json`), which library modules the
  test files import (`preload.json`), and which tests loop over independent units
  (`units.json`).

`selector.py` parses each diff and maps its changed lines through those maps (a changed line
stands for its whole statement; a function signature stands for its body; an inserted line
stands for its neighbours). That gives the covering tests: a superset of what can fail.

Configuration files: `conftest.py`, `pytest.ini`, or a `pyproject.toml` / `setup.cfg` /
`tox.ini` that has a pytest section before or after the patch select every test; without a
pytest section they select none (the suite is run from the checkout, not from an installed
package). A new top-level module or package selects every test, since it can shadow an import.

## 2. Run to decide (executing the patched code)

The image contains the pinned sqlglot checkout and the packages of `requirements.lock`.
For a patch that edits files under `sqlglot/` or `tests/` in place, the selector

1. applies the diff in memory and builds a throw-away copy of the checkout in `/tmp` made of
   symlinks, with only the patched files real;
2. forks a child that imports sqlglot fresh from that copy and runs covering tests with
   `unittest`, exactly as the suite defines them (`SKIP_INTEGRATION=0`, class fixtures,
   subtests; a failed subtest fails its test), cheapest first, reporting each result back
   over a pipe;
3. removes from the answer every test that was run and passed.

Which tests are run is a cost decision: CPU time is charged, so a test is run only when the
CPU it is expected to cost is clearly less than what leaving it out of the answer saves.
The estimate comes from the recorded seconds and is corrected as the batch goes by the CPU
the runs actually used (commits with the smallest covering sets go first), so the model
adapts to the machine. Tests that are not run stay in the answer. A run stops at the first failure unless
finishing is cheaper than what it can still save. Tests recorded at 3.6 s or more are never
run here (except unit-restricted, below).

### Tests that loop over units

Three expensive tests run one body once per unit, each in its own subtest:
`TestGenerator::test_generator_memory` (6.7 s, one unit per dialect) and
`TestExecutor::test_execute_tpcds` / `test_execute_tpch` (21 s / 4 s, one unit per fixture
query). `map/units.json` names what each loops over (an Enum in the test module, a list on
the test class), and the coverage map holds one extra trace per unit, taken on the pinned
commit. A unit the change does not reach cannot change its outcome. When a change reaches
only a few units, the child runs the test's own code with the loop restricted to those
units, which costs that share of its time; if they pass, the test is dropped like any other
test that was run and passed, and if one fails the test is kept.

### Evidence rule for the TPC executor tests

`TestExecutor::test_execute_tpcds` (21 s) and `test_execute_tpch` (4 s) replay whole TPC
query sets through parser, optimizer, planner and executor; when a change reaches most of
their queries they cost more CPU to run than they cost to keep. The selector may then run *every
other* covering test on the patched code (only if that is cheap enough, and only if there
are at least five). If any of them fails, nothing is dropped beyond the tests that passed.
If all of them pass, the two TPC executor tests are left out as well.

This rule is a measured bet, not a proof. `tools/README.md` describes the measurement:
single-line mutants were generated on lines that `test_execute_tpcds` executes
(executor, planner, optimizer, parser, generator and dialect files), every other covering
test was run on each mutant, and the TPC test itself was run whenever all of them passed.
Mutants that break anything were caught by a cheaper covering test almost immediately; in
none of the mutants where all cheaper covering tests passed did the TPC test fail (counts
in `tools/README.md`).

Safety nets, all falling back to the static selection or to something larger:

- the patch does not apply exactly, touches other paths, adds/deletes/renames files, or is
  binary: static selection;
- the changed block no longer compiles: every test that imports the file;
- the edit touches a statement executed at import time (module or class level, a `def`
  header): the child first imports the patched module; if that fails or hangs, every test
  that imports the file;
- a class or module fixture fails, the child dies or exceeds its time allowance, or the
  global time budget is spent: nothing is removed.

Two things differ from a pytest run, both only in how the same work is scheduled: the child
is pinned to one core, and a test that submits work to a `ProcessPoolExecutor` gets it done
in the child itself (same function, arguments and results pickled as a pool would), so no
worker process has to start and import everything again.

The child's stdout and stderr go to `/dev/null`; only the JSON lines reach stdout.

## Rebuilding the maps

See `tools/README.md`.
