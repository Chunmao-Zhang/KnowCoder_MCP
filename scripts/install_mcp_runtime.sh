#!/bin/sh

set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
config_root=${XDG_CONFIG_HOME:-"$HOME/.config"}/knowcoder-mcp

if ! command -v uv >/dev/null 2>&1; then
    echo "knowcoder-mcp: uv is required. Install it from https://docs.astral.sh/uv/" >&2
    exit 69
fi

uv tool install --force --python 3.12 "$source_root"
mkdir -p "$config_root"
if [ ! -f "$config_root/config.py" ]; then
    install -m 600 "$source_root/config.py.example" "$config_root/config.py"
fi

printf '%s\n' "Installed knowcoder-mcp."
printf '%s\n' "Edit $config_root/config.py, then run: knowcoder-mcp doctor"
