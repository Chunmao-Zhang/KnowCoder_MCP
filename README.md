<h1 align="center">
  <img src="assets/knowcoder-logo.svg" alt="KnowCoder" width="52" align="absmiddle">
  KnowCoder MCP
</h1>

KnowCoder MCP is a local MCP Server that turns a deep-research question into a reusable, source-grounded Workspace. It keeps the research plan, source material, Schema, entities, relations, provenance, and final report together. A completed Workspace can be extended later without repeating accepted work.

The repository contains the MCP Server, background task runtime, research Subagents, validators, storage layer, and read-only Problem and Schema Review pages. It does not contain the KnowCoder chat frontend or Solver.

A global registration stores Workspaces in one user-level KnowCoder data directory. Codex, Claude Code, and Claude Desktop/Work on the same computer can therefore find and extend the same Workspace by ID without a project-path setting.

## What happens during a task

1. The host Agent starts a Workspace task.
2. KnowCoder analyzes the question and pauses at Problem Review.
3. The user reviews the scope and plan in a durable local HTML page, then confirms or requests changes in the Agent conversation.
4. KnowCoder builds a Schema and pauses at Schema Review.
5. After confirmation, KnowCoder collects evidence, extracts entities and relations, validates the result, and publishes the Workspace.
6. The host Agent reads the Workspace and answers the original question.

Long stages run as background tasks. The host performs one serial wait at a time. A task waiting for user review consumes no model or search requests. Concurrent conversations receive separate task IDs, while an explicit Workspace ID lets a later task extend the same Workspace.

## Requirements

- macOS or Windows.
- Git.
- [`uv`](https://docs.astral.sh/uv/).
- A research model exposed through an OpenAI-compatible API.
- An extraction model exposed through an OpenAI-compatible API.
- A [Serper](https://serper.dev/) API key.
- An MCP host such as Codex, Claude Code, or Claude Desktop/Work.

## Installation option 1: install manually

This path uses only terminal commands. The local installation check does not call an LLM, the model APIs, or Serper.

### 1. Install `uv` when needed

macOS:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Restart the terminal after installing `uv`, then verify it:

```bash
uv --version
```

### 2. Download or update the repository

Use the same user-level source directory on every computer. Repeating these commands updates an existing installation
without touching API configuration or Workspace data.

macOS:

```bash
SOURCE_DIR="$HOME/.local/share/knowcoder-mcp/source"
mkdir -p "$(dirname "$SOURCE_DIR")"
if [ -d "$SOURCE_DIR/.git" ]; then
  git -C "$SOURCE_DIR" pull --ff-only origin main
else
  git clone https://github.com/Chunmao-Zhang/KnowCoder_MCP.git "$SOURCE_DIR"
fi
cd "$SOURCE_DIR"
```

Windows PowerShell:

```powershell
$SourceDir = Join-Path $env:LOCALAPPDATA "knowcoder-mcp\source"
if (Test-Path (Join-Path $SourceDir ".git")) {
    git -C $SourceDir pull --ff-only origin main
} else {
    git clone https://github.com/Chunmao-Zhang/KnowCoder_MCP.git $SourceDir
}
Set-Location $SourceDir
```

### 3. Install the command

macOS:

```bash
./scripts/install_mcp_runtime.sh
```

Windows PowerShell:

```powershell
.\scripts\install_mcp_runtime.ps1
```

The installer force-reinstalls the checked-out source with `uv tool`, downloads Python 3.12, and creates an isolated environment. It does not use an older
system Python. It installs from official PyPI so an outdated system package mirror cannot silently provide an
incomplete environment. To use another complete package index explicitly, set `KNOWCODER_PACKAGE_INDEX` before
running the script. The installer also creates the user configuration file when it is missing. Reinstallation does
not overwrite an existing configuration.

Configuration locations:

- macOS: `~/.config/knowcoder-mcp/config.py`
- Windows: `%APPDATA%\knowcoder-mcp\config.py`

If the terminal cannot find `knowcoder-mcp` after installation, run `uv tool update-shell`, restart the terminal, and try again.

### 4. Configure the APIs

Open the user `config.py` and fill these values:

```python
RESEARCH_MODEL = {
    "api_key": "your-research-model-api-key",
    "base_url": "https://your-provider.example/v1",
    "model": "your-research-model-name",
}

EXTRACTION_MODEL = {
    "api_key": "your-extraction-model-api-key",
    "base_url": "https://your-provider.example/v1",
    "model": "your-extraction-model-name",
}

SERPER_API_KEY = "your-serper-api-key"
```

The two model sections may use the same provider and key. Keep real secrets in this user configuration file. Do not add them to the repository or MCP host configuration.

### 5. Verify the installation without an LLM

The installer installs Crawl4AI, downloads its compatible Chromium browser, runs `crawl4ai-doctor`, and renders a
local HTML page through KnowCoder's own Doctor. The first installation can therefore take longer than a normal
Python package install. The upstream `crawl4ai-doctor` opens `https://crawl4ai.com` once to verify a real webpage;
KnowCoder's `doctor --local` uses only local HTML. Crawl4AI runs locally and does not require an API key.

```bash
knowcoder-mcp --version
knowcoder-mcp doctor --local
```

A successful local check ends with:

```text
PASS local installation; no model or search API was called
```

`doctor --local` also starts Chromium once and verifies that Crawl4AI can render a local page. It makes no model,
Serper, or external webpage request. `WARN configuration incomplete` means the program is installed correctly but
one or more API settings are still empty. Complete `config.py` before starting a research task.

To verify the configured external services later, you may run `knowcoder-mcp doctor`. That optional command makes one small request to each configured model and one Serper request.

### 6. Register the MCP Server

First find the absolute executable path. This avoids PATH differences in desktop applications.

macOS:

```bash
command -v knowcoder-mcp
```

Windows PowerShell:

```powershell
(Get-Command knowcoder-mcp).Source
```

Replace `/ABSOLUTE/PATH/TO/knowcoder-mcp` below with the real absolute executable path. Register the Server once at user scope. No Workspace path is required. The default runtime location is:

- macOS: `~/.local/share/knowcoder-mcp/.knowcoder_workspace/`
- Windows: `%LOCALAPPDATA%\knowcoder-mcp\.knowcoder_workspace\`

All supported hosts on the same user account share this location. Runtime files remain local and are not written into the cloned repository.

#### Codex

Add this user-level entry to `~/.codex/config.toml`:

```toml
[mcp_servers.knowcoder_workspace_builder]
command = "/ABSOLUTE/PATH/TO/knowcoder-mcp"
args = ["serve"]
startup_timeout_sec = 30
tool_timeout_sec = 60
```

#### Claude Code

```bash
claude mcp add --scope user knowcoder_workspace_builder -- /ABSOLUTE/PATH/TO/knowcoder-mcp serve
```

#### Claude Desktop or Claude Work

Open **Settings → Connectors → Add custom connector** and enter:

- Name: `knowcoder_workspace_builder`
- Command: the absolute `knowcoder-mcp` executable path
- Arguments: `serve`

For hosts that accept a JSON MCP configuration, use:

```json
{
  "mcpServers": {
    "knowcoder_workspace_builder": {
      "command": "/ABSOLUTE/PATH/TO/knowcoder-mcp",
      "args": ["serve"]
    }
  }
}
```

Restart the host. Open its MCP or tools panel and verify that `knowcoder_workspace_builder` is connected and exposes exactly these six tools:

- `start_workspace_task`
- `wait_for_task_update`
- `submit_review_decision`
- `read_workspace`
- `find_workspace_tasks`
- `stop_task`

This connection and tool-list check does not require sending a question to an LLM.

## Installation option 2: ask an Agent to install it

Copy the prompt below into a local coding Agent. Fill any values you already have. Empty values are allowed: the Agent must still finish the installation and explain how to complete the configuration later.

```text
Install KnowCoder MCP for my current user from:
https://github.com/Chunmao-Zhang/KnowCoder_MCP

Configuration I can provide now:
- Research model API key: <OPTIONAL_API_KEY>
- Research model Base URL: <OPTIONAL_BASE_URL>
- Research model name: <OPTIONAL_MODEL_NAME>
- Extraction model API key: <OPTIONAL_API_KEY>
- Extraction model Base URL: <OPTIONAL_BASE_URL>
- Extraction model name: <OPTIONAL_MODEL_NAME>
- Serper API key: <OPTIONAL_SERPER_API_KEY>

Role
Install and register the released KnowCoder MCP without changing unrelated host settings.

Workflow
1. Detect macOS or Windows.
2. Install Git or uv only when missing. Use each project's official installation method.
3. Use exactly one user-level source directory: `~/.local/share/knowcoder-mcp/source` on macOS or
   `%LOCALAPPDATA%\knowcoder-mcp\source` on Windows. Clone the repository there when missing. When it already exists,
   run `git pull --ff-only origin main`. Stop and report any Git conflict instead of installing stale source.
4. Run the repository installation script for this operating system. Let `uv` provision Python 3.12, install
   Crawl4AI from official PyPI, download Chromium, and run both Crawl checks. Crawl4AI requires no API key. Use a
   custom `KNOWCODER_PACKAGE_INDEX` only when that index contains every current dependency.
5. Create the user config.py from config.py.example when it is missing.
6. Write every provided API value to the user config.py. Keep secrets out of the repository, terminal output, chat output, and host MCP configuration.
7. When any API value is empty, complete the installation anyway. At the end, state exactly which values are missing and offer me two choices: give the values to you now, or edit the reported user config.py path myself.
8. Find the absolute knowcoder-mcp executable path.
9. Register one user-level stdio MCP Server named knowcoder_workspace_builder in the current host. Use the absolute executable path and the single argument `serve`. Preserve every unrelated host setting. Do not bind the registration to one project directory.
10. Run `knowcoder-mcp --version` and `knowcoder-mcp doctor --local`. Confirm that Crawl4AI and Chromium pass. This local test must not call any model, search API, or external webpage.
11. Restart or reload the MCP connection when the host supports it. Inspect the host's MCP tool list and verify that the Server exposes exactly six tools: start_workspace_task, wait_for_task_update, submit_review_decision, read_workspace, find_workspace_tasks, and stop_task.
12. If all API values are present, run `knowcoder-mcp doctor` once to test the configured model and Serper services. If values are missing, skip this network test and report that research cannot start until config.py is completed.

Completion report
- Report whether package installation, local diagnosis, host registration, and six-tool discovery passed separately.
- Report the repository path, executable path, user config.py path, and host configuration file changed.
- Report missing configuration fields plainly.
- Report every failure with the failed step and original error. Do not silently substitute another model, service, path, or configuration scope.
```

## Using KnowCoder MCP

Ask a research question naturally. For work that needs deep external research, the host Agent can use KnowCoder to build a structured Workspace. You do not need to mention MCP in the question.

At Problem Review and Schema Review, the Agent should summarize the result and provide the local review-page link. Review the page, then reply in the same conversation with a confirmation or a natural-language revision. The review page is read-only and durable; it does not continue the task by itself.

During long-running stages, brief progress is reported when the active Subagent changes or an error occurs. When the Workspace is complete, the Agent reads its evidence and produces the final response.

Evidence collection processes the confirmed research steps in order. Each step reviews Search candidates, Fetches promising pages with bounded internal concurrency, and records inaccessible gaps without discarding successful sources. Five consecutive external Search failures stop the stage; any successful Search resets that failure streak.

## Public tools

| Tool | Purpose |
| --- | --- |
| `start_workspace_task` | Start new research, extend a Workspace, or recover a failed task. |
| `wait_for_task_update` | Wait once for background progress. Only one wait should be active per task. |
| `submit_review_decision` | Confirm or revise the Problem or Schema checkpoint. |
| `read_workspace` | Read a completed Workspace resource with pagination. |
| `find_workspace_tasks` | Find tasks and Workspaces for recovery or continuation. |
| `stop_task` | Stop an active task while preserving its last published Workspace. |

## Workspace layout

Runtime data stays inside the shared user-level `.knowcoder_workspace/` described in the installation section. A published Workspace contains:

```text
workspace/
  README.md                 # Human-readable Workspace guide and summary
  workspace.yaml            # Machine-readable Workspace metadata
  review/                   # Durable Problem and Schema Review pages
  ontology/
    README.md               # Schema guide
    types.py                # Generated entity and relation types
    loader.py               # Workspace loading helper
    schema.json             # Validated Schema
  data/
    entities.jsonl          # Extracted entities
    relations.jsonl         # Extracted relations
    source_chunks.jsonl     # Chunk index and provenance
    manifest.json           # Data-file manifest
    source/                 # Full collected source documents
```

Incremental research keeps the same Workspace ID. Validated updates are published atomically, so a failed run does not replace the last accepted Workspace.

## Troubleshooting

### `knowcoder-mcp` is not found

Run `uv tool update-shell`, restart the terminal, and repeat `knowcoder-mcp --version`. Desktop hosts should use the absolute executable path returned by `command -v knowcoder-mcp` or `(Get-Command knowcoder-mcp).Source`.

### Configuration is incomplete

Open the user `config.py` path shown by `knowcoder-mcp doctor --local`. Fill every empty API key, Base URL, and model name. KnowCoder fails fast and reports the missing field; it does not silently choose another provider.

### Crawl4AI or Chromium setup fails

Run the platform installation script again and keep the original `playwright install chromium` or `crawl4ai-doctor` error. The
MCP cannot fetch HTML pages until `knowcoder-mcp doctor --local` prints `PASS Crawl4AI and Chromium`. Crawl4AI does
not use an API key, so adding a key will not fix a missing browser.

### The Server is installed but absent from the host

Confirm that registration is user-level and the executable path is absolute. Restart the host after editing its MCP configuration.

### A task is waiting

Open the returned review page. Confirm or revise the checkpoint in the original Agent conversation. Waiting for review is expected and consumes no API requests.

Generated Workspaces, local environments, caches, build output, user configuration, tests, and internal design records are excluded from publication.
