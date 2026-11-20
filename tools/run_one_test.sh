#!/bin/sh
cd /w/sqlglot
TRACE_OUT=/w/out2/$1.json TRACE_ROOT=/w/sqlglot/ SKIP_INTEGRATION=0 PYTHONPATH=/w/sqlglot:/w/site2 \
  python -m pytest -q -x -p no:cacheprovider "$2" > /w/out2/$1.log 2>&1
echo "$1 $?" >> /w/out2/status.txt
