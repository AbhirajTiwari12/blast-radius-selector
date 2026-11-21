Maps are built in the labeling image (`python:3.13-slim` at the digest in the Dockerfile) with
`requirements.lock` installed and sqlglot checked out at the pinned commit:

1. run every test of `tests.json` alone in its own process with `sitecustomize.py` on the path
   (`run_one_test.sh`): it records the lines run inside functions, the lines run at import,
   the classes instantiated, the data files opened, one trace file per process;
2. `python tools/build_map.py OUT SQLGLOT_CHECKOUT map tests.tsv` writes `map/ids.json`,
   `map/files/*.json` (line -> tests, import-time lines, lines inside strings) and
   `map/fixtures.json`;
3. `map/procs.json`: tests whose run left more than one extra trace file (they use a process
   pool); `map/preload.json`: library modules imported by every test file (preloaded before
   forking) and the test files that import duckdb/pandas/numpy.

4. units: for a test listed in `map/units.json`, `unit_run.py` runs it with its loop restricted
   to one unit, under the same tracer; those traces are added to step 2 as extra entries
   named `<test id>[<unit>]` (`build_map.py OUT1,OUT2 ...`). They are not tests of
   `tests.json`; the selector uses them only to tell which units a change reaches.

   For the TPC executor tests `trace_queries.py` traces every fixture query in one process
   (parse, pickle round trip, execute, compare, plus the shared import and fixture work) and
   records what each query costs.

`measure_cpu.py` measures the CPU a test costs when run by the selector (used to choose the
constants of the cost model in `selector.py`).

## Validation (not used at run time)

Nothing below is read by the selector; it is how the rules in `selector.py` were checked.

- Mutants: single-line edits of the pinned sources (operator, constant and keyword swaps in
  function bodies, class-level constants, module-level statements, fixture edits, comment
  insertions, multi-file mixes), labeled by running pytest on every test whose trace touched
  a changed file.
- Evidence rule: mutants on lines executed by `test_execute_tpcds`; each row is one mutant
  whose covering set contains the TPC test and is cheap enough to run.

  | files mutated | mutants | a cheaper covering test fails | all cheaper pass, TPC passes | all cheaper pass, TPC fails |
  | --- | --- | --- | --- | --- |
  | executor/, planner | 105 | 78 | 22 | 0 |
  | optimizer/ | 54 | 45 | 7 | 0 |
  | generator, dialects, transforms | 21 | 20 | 1 | 0 |
  | parser, tokens, expressions | 4 | 3 | 1 | 0 |
  | class-level lines | 2 | 1 | 1 | 0 |

  (11 further mutants broke a class fixture or the import; those keep the static answer.)
  Mutants that fail a cheaper covering test failed it within the first 0.3 recorded seconds
  in all but a handful of cases. 32 clean runs with no TPC failure bound the miss rate of the
  rule at about 9 % (95 %); the break-even against the 300 s miss charge is about 7 %, the
  expected rate about 3 %.
