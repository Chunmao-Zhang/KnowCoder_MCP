#!/bin/sh

set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
config_root=${XDG_CONFIG_HOME:-"$HOME/.config"}/knowcoder-mcp
package_index=${KNOWCODER_PACKAGE_INDEX:-"https://pypi.org/simple"}

if ! command -v uv >/dev/null 2>&1; then
    echo "knowcoder-mcp: uv is required. Install it from https://docs.astral.sh/uv/" >&2
    exit 69
fi

UV_DEFAULT_INDEX="$package_index" uv tool install --force --python 3.12 "$source_root"
tool_root=$(UV_DEFAULT_INDEX="$package_index" uv tool dir)/knowcoder-mcp
"$tool_root/bin/playwright" install chromium
"$tool_root/bin/crawl4ai-doctor"
mkdir -p "$config_root"
if [ ! -f "$config_root/config.py" ]; then
    install -m 600 "$source_root/config.py.example" "$config_root/config.py"
fi
"$tool_root/bin/knowcoder-mcp" doctor --local

printf '%s\n' "Installed knowcoder-mcp."
printf '%s\n' "Edit $config_root/config.py, then run: knowcoder-mcp doctor"
