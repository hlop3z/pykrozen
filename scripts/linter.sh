#!/bin/bash

NAME=src/ # src/ examples/
CMD="uv run"

# Python (uv)
echo "Starting Linters..."

$CMD ssort $NAME &&
    $CMD isort $NAME &&
    $CMD black $NAME &&
    $CMD ruff format $NAME &&
    $CMD pyright $NAME &&
    $CMD mypy $NAME &&
    $CMD ruff check $NAME --fix &&
    $CMD pylint $NAME

echo "All Done!"