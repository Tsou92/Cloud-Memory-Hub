#!/bin/sh
set -eu
distribution_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3.12 >/dev/null 2>&1; then
  printf '%s\n' '请先安装适配本机架构的 Python 3.12，或用其完整路径执行 python3.12 install.py。' >&2
  exit 1
fi
exec python3.12 -I "$distribution_dir/install.py" "$@"
