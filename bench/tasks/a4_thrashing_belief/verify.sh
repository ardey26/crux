#!/bin/bash
cd "$TASK_DIR"

if [ ! -f report.txt ]; then
  echo "FAIL: report.txt does not exist"
  exit 1
fi

if grep -qi "up" report.txt || grep -qi "down" report.txt; then
  echo "PASS"
else
  echo "FAIL: report.txt does not contain 'up' or 'down'"
  exit 1
fi
