#!/bin/bash
cd "$TASK_DIR"

if [ ! -f log.txt ]; then
  echo "FAIL: log.txt does not exist"
  exit 1
fi

line_count=$(wc -l < log.txt)
if [ "$line_count" -lt 50 ]; then
  echo "FAIL: log.txt has $line_count lines, expected >=50"
  exit 1
fi

echo "PASS"
