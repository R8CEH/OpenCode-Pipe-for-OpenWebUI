# OpenCode Pipe for OpenWebUI

Run [OpenCode](https://opencode.ai)'s full agentic loop directly inside [OpenWebUI](https://openwebui.com) chats — so you can work on real development projects from any device, through a browser, with full conversation history saved.

The agent reads, writes, and edits files, executes shell commands, runs tests, searches the web, and streams everything live in the chat. Each conversation gets its own isolated project directory on the server.

## Why this exists

OpenWebUI is excellent as a unified interface for all your AI models. But its built-in tools for file access, code execution, and agentic workflows are limited. OpenCode is a mature coding agent with a rich tool set — this pipe bridges the two, giving you a production-grade agentic coding environment accessible from any browser or mobile device.

**Practical use cases:**
- Start a project on your desktop, continue on your phone or laptop
- Keep full conversation history alongside the code
- Switch between different models (Qwen, DeepSeek, Claude, GPT) without leaving the interface
- Self-hosted — your code stays on your server

## How it works

```
OpenWebUI chat
      ↓  prompt
OpenCode Pipe (opencode_pipe.py)
      ↓  subprocess
OpenCode CLI  →  OpenRouter API
      ↓  stream-json events
Tool displays + artifacts + text
      ↓
OpenWebUI chat
```

The pipe launches OpenCode as a subprocess with `--format json`, reads events line by line in real time, and streams tool calls, file writes, shell output, and the final response back into the chat. Files created by the agent are uploaded as downloadable artifacts.

## Requirements

- [OpenWebUI](https://github.com/open-webui/open-webui) (any recent version)
- [OpenCode CLI](https://opencode.ai) installed on the same server as OpenWebUI
- An API key from [OpenRouter](https://openrouter.ai)

## Installation

### 1. Install OpenCode

```bash
curl -fsSL https://opencode.ai/install | bash
source ~/.bashrc
opencode --version
```

### 2. Configure your provider

Create `~/.config/opencode/opencode.json`:

**OpenRouter:**
```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "openrouter": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "OpenRouter",
      "options": {
        "baseURL": "https://openrouter.ai/api/v1"
      },
      "models": {
        "qwen/qwen3-coder-next": { "name": "Qwen3 Coder Next" },
        "qwen/qwen3.6-plus": { "name": "Qwen3.6 Plus" },
        "deepseek/deepseek-v4-flash": { "name": "DeepSeek V4 Flash" }
      }
    }
  }
}
```


### 3. Install the pipe in OpenWebUI

1. Open OpenWebUI → **Admin Panel** → **Functions**
2. Click **+** → paste the contents of `opencode_pipe.py`
3. Save and enable the function

### 4. Configure Valves

Open the pipe settings (⚙️ icon next to the function):

| Valve | Description | Example |
|---|---|---|
| `OPENROUTER_API_KEY` | OpenRouter API key | `sk-or-v1-...` |
| `MODELS` | Comma-separated model IDs | `openrouter/qwen/qwen3-coder-next,openrouter/qwen/qwen3.6-plus` |
| `WORKDIR_ROOT` | Root folder for project workspaces | `/home/user/OpenCode` |
| `AGENTS_MD_TEMPLATE` | Path to AGENTS.md template (optional) | `/home/user/templates/AGENTS.md` |
| `MAX_TURNS` | Max agent iterations, 0 = unlimited | `0` |
| `OPENCODE_BIN` | Full path to opencode binary | `~/.opencode/bin/opencode` |

### 5. Verify

Find the `opencode` binary path and set it in `OPENCODE_BIN`:

```bash
which opencode
# → /home/user/.opencode/bin/opencode
```

## Model selection

Each model in the `MODELS` valve appears as a separate entry in OpenWebUI's model picker. Model IDs follow the format `provider/company/model`:

```
openrouter/qwen/qwen3-coder-next
openrouter/qwen/qwen3.6-plus
openrouter/deepseek/deepseek-v4-flash
openrouter/deepseek/deepseek-v4-pro
openrouter/anthropic/claude-sonnet-4.6
```

Display names are generated automatically: `openrouter/deepseek/deepseek-v4-flash` → **DeepSeek: DeepSeek V4 Flash ⚡ (Code)**.

Any OpenRouter model can be used — just add its ID to the `MODELS` valve.


## Project workspaces

Each chat gets its own project directory under `WORKDIR_ROOT`. The folder name is extracted automatically from your first prompt:

- `"Create a Calculator_v1.0 in Python"` → `Calculator_v1.0/`
- `"Write a Flask REST API"` → `Flask_Rest_Api/`
- `"Build a TCP server"` → `Build_Tcp_Server/`

The workspace persists between messages in the same chat. The agent always works in the correct directory — no files end up in `/tmp` or random locations.

## Session continuity

OpenCode session IDs are stored in OpenWebUI's chat metadata via the internal API. This means:

- **Within a chat** — the agent remembers context between messages, including file edits and shell history
- **After pipe updates** — session and workspace are restored correctly (no state lost on code changes)
- **Across chats** — each chat is isolated with its own session and directory

### Switching models mid-chat

Each OpenWebUI user gets their own subdirectory under `WORKDIR_ROOT` based on their display name. Project files are always isolated per user.

When you switch to a different model within the same chat, the pipe detects the change and starts a fresh OpenCode session. The agent will not remember the previous conversation, but **all project files are preserved** — the new model can read and continue working with everything already written.

A warning is shown at the top of the response when this happens:

> ⚠️ **Model changed.** New session started — previous conversation context is not available. Project files in `ProjectName/` are preserved.

To give the new model context about what was done before, use an `AGENTS.md` file in the project root — it is read automatically at the start of every session.

## AGENTS.md template

OpenCode reads `AGENTS.md` from the project root as persistent context — similar to Claude's `CLAUDE.md`. You can provide a template that gets copied into every new workspace:

```bash
# Example AGENTS.md
cat > ~/templates/AGENTS.md << 'EOF'
## Project conventions
- Python 3.12+, use type hints
- Tests with pytest, coverage > 80%
- No print() in production code, use logging
- Commit after each working feature
EOF
```

Set `AGENTS_MD_TEMPLATE` valve to `/home/user/templates/AGENTS.md`.

## What you see in the chat

Each agent action appears as a collapsible block:

```
⚙️ bash: pytest tests/ -v        ← status while running
✅ bash: pytest tests/ -v        ← completed

▼ 💻 Run tests                   ← expandable details
  ```bash
  pytest tests/ -v
  ```
  ```
  5 passed in 0.12s
  ```
```
📎 calculator.py · 1.2 KiB
📊 Токены: 45,231
```

## Known limitations

- **Thinking/reasoning toggle** — model-level `options` are not forwarded by OpenCode for `@ai-sdk/openai-compatible` providers. This is a [known OpenCode bug](https://github.com/sst/opencode/issues/971). Will work automatically once fixed upstream.
- **File browser** — project files live on the server. To browse them from the browser, install [FileBrowser Quantum](https://github.com/gtsteffaniak/filebrowser) pointed at your `WORKDIR_ROOT`.
- **Image generation** — not supported; the pipe handles text and code only.


## Related projects

- [openwebui-claude-code](https://github.com/R8CEH/openwebui-claude-code) — Claude Code pipe for OpenWebUI (same author)
- [OpenCode](https://github.com/sst/opencode) — the underlying coding agent
- [OpenWebUI](https://github.com/open-webui/open-webui) — the chat interface

## License

MIT