#!/usr/bin/env bash
# Hardware-independent tests; never load a module or write sysfs.
set -euo pipefail
cd -- "$(dirname -- "$(realpath -- "$0")")/.."
test_dir=$(mktemp -d)
trap 'rm -rf -- "$test_dir"' EXIT
${CC:-cc} -std=gnu11 -Wall -Wextra -Werror -g -O1 \
    -fsanitize=address,undefined -fno-omit-frame-pointer -Idriver \
    tests/test_curve.c -o "$test_dir/test_curve"
"$test_dir/test_curve"
SCHENKER_POWER_PROFILE=balanced SCHENKER_CHARGING_PROFILE= \
    python3 -B -m unittest discover -s tests -p 'test_*.py' -v
bash -n install.sh tools/trial.sh tests/run.sh
