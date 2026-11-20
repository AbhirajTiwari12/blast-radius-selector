# Blast Radius selector

Picks, for each patch against sqlglot at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, the tests
that can observe the change, and splits them across workers.

## How it works

`map/` is built once from the pinned commit only (`tools/`):

- every test is run alone in its own process and the lines it executes are recorded, so each
  source line maps to the tests that run it (module- and class-level lines map to the tests
  that import the module);
- the non-Python files under `tests/` that each test opens are recorded, so a fixture maps
  to the tests that read it.

At run time `selector.py` parses each diff, maps its changed lines through those maps
(a changed line stands for its whole statement; a function signature stands for its body;
an inserted line stands for its neighbours), and balances the selected tests over the number
of workers that minimises busiest-worker time plus the per-worker charge.

There is no table from patches or kinds of edit to tests: the output depends only on the
diff's line numbers and the coverage maps.

## Rebuilding the maps

See `tools/README.md`.
