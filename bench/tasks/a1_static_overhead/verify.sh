#!/bin/bash
cd "$TASK_DIR"

if [ ! -f summary.txt ]; then
  echo "FAIL: summary.txt does not exist"
  exit 1
fi

char_count=$(wc -c < summary.txt)
if [ "$char_count" -le 50 ]; then
  echo "FAIL: summary.txt has $char_count chars, expected >50"
  exit 1
fi

echo "PASS"
