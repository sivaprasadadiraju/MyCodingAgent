# myCodingAgent for VS Code Copilot Chat

Use your local **myCodingAgent** Python agent directly from the VS Code
Copilot chat window as a chat participant: `@myagent`.

## Install (developer mode)

1. Copy or keep the `myCodingAgent/` folder inside (or anywhere relative to)
   your workspace.
2. Open this folder in VS Code:
   ```
   code myCodingAgent\vscode_extension
   ```
3. In that window's terminal:
   ```
   npm install -g @vscode/vsce
   vsce package
   code --install-extension mycodingagent-chat-1.0.0.vsix
   ```
   Or for quick testing without packaging: open the `vscode_extension`
   folder, press **F5** (Run Extension) — a new VS Code window opens with
   the extension loaded.

> Requires VS Code 1.90+ with Copilot Chat. GitHub Copilot Chat exposes the
> Chat Participants API that this extension uses.

## Settings (optional)

| Setting | Default | Description |
|---|---|---|
| `myCodingAgent.pythonPath` | `python` | Python executable to run the agent |
| `myCodingAgent.agentFolder` | auto-detect | Folder containing `agent.py` and `llm.py`; auto-detected from the opened workspace's `myCodingAgent/` directory |
| `myCodingAgent.timeoutSeconds` | `600` | Max seconds per task |
| `myCodingAgent.llmBaseUrl` | empty | Optional OpenAI-compatible API base URL |
| `myCodingAgent.llmModel` | empty | Model name for the alternate API |
| `myCodingAgent.llmApiKeyEnv` | `MYCODINGAGENT_LLM_API_KEY` | Environment variable name containing the optional API key |

Coding requests that receive prose instead of a tool action are retried as
edits; the agent does not report completion without a workspace write, and
Copilot Chat reports explicitly when no files were changed. If the
extension cannot find the Python source folder, click **Locate
myCodingAgent source folder** in the Copilot Chat response and choose the
folder containing `agent.py` and `llm.py`. The selection is saved for the
current workspace; retry the request afterward.

OxAlpha can impose a server-side verification or usage checkpoint. The agent
does not bypass it. To avoid depending on OxAlpha, configure `llmBaseUrl` and
`llmModel` for another OpenAI-compatible provider. For a local Ollama server,
for example, use `http://localhost:11434/v1` and an installed model name; no API
key is needed. For a hosted provider, put its key in the named environment
variable (never in settings.json), then restart VS Code.

## Usage in Copilot chat

```
@myagent build a flask todo app with tests # full autonomous mode
@myagent /edit rename function foo to bar in main.py
@myagent /ask what does database.py do
```

- **default** — reads/writes files and runs permitted project checks. Commands
   require approval with buttons directly in the Copilot Chat response; no
   approval dialog window is opened. File edits are applied automatically in
   trusted workspaces and shown as diffs in the chat, so there is no Apply file
   change button to wait on. Inline code in shell commands (such as
   `python -c`) and chained shell commands are blocked. The agent must put code
   in workspace files and run those files or the project's test command.
   Step, read, write, and run activity is shown in the chat progress stream.
- `/edit` — file edits only, no shell commands.
- `/ask` — answers only, never changes files.
- If OxAlpha requests verification, the run pauses and offers to open its
   verification page or alternate-provider settings. The extension does not
   attempt to bypass human verification.

The agent works on the **first workspace folder** as its project root.
Conversation memory persists per workspace in VS Code extension storage.

## Files

- `extension.js` — chat participant, spawns the Python agent, streams results
- `chat_driver.py` — one-shot bridge: runs `agent.run_agent()`, prints JSON
- `package.json` — extension manifest (`chatParticipants` contribution)
