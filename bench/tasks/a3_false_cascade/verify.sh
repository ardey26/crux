#!/bin/bash
cd "$TASK_DIR"

python -m pytest tests/ -q --tb=short
