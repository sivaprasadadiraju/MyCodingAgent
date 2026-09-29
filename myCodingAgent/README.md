# myCodingAgent — Copilot-style coding assistant (by OxAlpha)

A terminal-based assistant that works like GitHub Copilot Chat in VS Code:
you talk to it in plain English, it reads your workspace, writes/edits code,
runs it, fixes errors, and reports back.

---

## 1. Quick start

Open a terminal in the project folder and run:

```bat
python myCodingAgent\cli.py
```

You get an interactive chat prompt (`you>`). Type requests in plain English.
Type `exit` (or `quit` / `q`) to leave.

One-shot usage (no interactive chat):

```bat
python myCodingAgent\cli.py "build me a flask todo app with sqlite"
python myCodingAgent\cli.py /ask "how does the agent loop work?"
python myCodingAgent\cli.py /edit "add error handling to app.py"
```

---

## 2. The three modes (like Copilot Chat modes)

| Mode   | Command   | What it does |
|--------|-----------|-----------------------------------------------------|
| ASK    | `/ask`    | Answer questions about your code. Read-only — never changes files. |
| EDIT   | `/edit`   | Makes targeted edits to named files. Does NOT run commands. |
| TASK   | `/task` or `/agent` | Full autonomous mode (default): create → run → fix loop until it works. |

Example session:

```
you> /ask what does database.py do?
you> /edit add a /health route to app.py
you> /task build a login page and make sure the server runs
```

The current mode stays active until you switch with a slash command.
`/task` is the default for plain messages.

---

## 3. Slash commands

| Command | Effect |
|---------|---------------------------------|
| `/ask <q>`    | switch to ASK mode (optionally with a question) |
| `/edit <i>`   | switch to EDIT mode (optionally with an instruction) |
| `/task <i>`   | switch to TASK mode (optionally with a task) |
| `/files`      | list workspace files the agent can see |
| `/new`        | start a fresh conversation (clears memory) |
| `/clear`      | clear the terminal screen |
| `/help`       | show the built-in help |
| `exit`        | quit |

---

## 4. Pointing at files with @

Mention a file with `@filename` and the agent will read it first:

```
you> @cli.py explain what main() does
you> @app.py @config.py why does the app fail to start?
you> /edit @app.py add CORS support
```

---

## 5. What TASK mode does (the autonomous loop)

When you give it a build task, the agent:
1. Reads relevant workspace files.
2. Writes/creates the code (with proper indentation).
3. **Runs** it via shell commands.
4. If the run shows errors, reads the error output, fixes the code, re-runs.
5. Repeats until it runs clean, then reports: files changed,
   commands executed, and a summary.

Example:

```
you> /task create a flask API with a /users endpoint backed by sqlite
```

---

## 6. Safety & backups

- Every file that gets overwritten is backed up automatically to
  `.agent_backups\` with a timestamp (e.g. `cli_20260910_075850.py`).
- ASK mode never modifies anything — safe for exploring unfamiliar code.
- EDIT mode never runs shell commands — safe when you only want diffs.
- The agent asks for confirmation before every file write and shell command.
- Existing files are backed up automatically to `.mca_backups\` before writing.
- If OxAlpha requires Turnstile/human verification, the agent pauses and
  reports that state instead of retrying or claiming verification succeeded.
  This server-side limit cannot be removed or bypassed by a local code change.
  To use a different OpenAI-compatible provider, set
  `MYCODINGAGENT_LLM_BASE_URL` and `MYCODINGAGENT_LLM_MODEL`; set
  `MYCODINGAGENT_LLM_API_KEY` in the environment if that provider needs a key.
  Local Ollama can be used without a key. Otherwise complete the check manually
  at `https://oxalpha.com/chat` and retry later.
- In coding mode, prose or a premature “done” response is retried as an edit
  request. If the model still does not return a file-write action, the agent
  reports that no files changed instead of implying the task succeeded.

---

## 7. Tips

- Be specific: "add a DELETE /jobs/<id> route to jobs_portal.py" beats "improve the API".
- Use `/new` between unrelated tasks so old context doesn't confuse it.
- Use `/ask` first to understand a codebase, then `/task` to change it.
- If a long task stops early (max steps), just say "continue".
