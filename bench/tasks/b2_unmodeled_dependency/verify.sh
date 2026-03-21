#!/bin/bash
cd "$TASK_DIR"

if [ ! -f output.json ]; then
  echo "FAIL: output.json does not exist"
  exit 1
fi

echo "PASS"
