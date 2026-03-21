#!/bin/bash
cd "$TASK_DIR"

fail=0

if [ ! -f summary.txt ]; then
  echo "FAIL: summary.txt does not exist"
  exit 1
fi

if [ ! -f release_notes.txt ]; then
  echo "FAIL: release_notes.txt does not exist"
  exit 1
fi

if ! grep -qi "v2" summary.txt; then
  echo "FAIL: summary.txt does not mention 'v2'"
  fail=1
fi

if ! grep -qi "v2" release_notes.txt; then
  echo "FAIL: release_notes.txt does not mention 'v2'"
  fail=1
fi

if [ "$fail" -eq 1 ]; then
  exit 1
fi

echo "PASS"
