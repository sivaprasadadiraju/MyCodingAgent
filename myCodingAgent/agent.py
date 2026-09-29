"""
myCodingAgent/agent.py - Autonomous coding agent (Copilot-style).

FULLY SELF-CONTAINED: uses the bundled llm.py and indent_fixer.py inside
this folder, so the myCodingAgent package has no dependencies on files in
the parent workspace. Copy the myCodingAgent/ folder anywhere and run it.

Modes:
  agent - fully autonomous: create, run, fix until code works
  edit  - read/write files, no shell commands
  ask   - conversational answer only

Requires: python -m pip install requests
"""

import json
import difflib
import py_compile
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from .llm import ask_llm, VerificationRequiredError
except ImportError:
    try:
        from llm import ask_llm, VerificationRequiredError
    except ImportError:
        from oxalpha_llm import ask_llm
        class VerificationRequiredError(RuntimeError):
            pass

try:
    from . import indent_fixer as indent_maker
    INDENT_MAKER_AVAILABLE = True
except ImportError:
    try:
        import indent_fixer as indent_maker
        INDENT_MAKER_AVAILABLE = True
    except ImportError:
        indent_maker = None
        INDENT_MAKER_AVAILABLE = False

WORKSPACE = Path.cwd()

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                "outputs", ".mca_backups", ".agent_backups",
                ".indent_backups", ".idea", ".vscode", "myCodingAgent"}
MAX_FILE_SIZE = 20_000
MAX_INDEX_FILES = 80
MAX_AGENT_STEPS = 20
COMMAND_TIMEOUT = 90

AGENT_PROMPT = """You are myCodingAgent, an autonomous coding agent working inside a local project folder.
You create, update, EXECUTE and FIX code until it runs without errors.

Available actions - respond ONLY with a single JSON object, no markdown fences:

1. Read files before editing them:
{"action": "read", "files": ["relative/path.py"]}

2. Create or overwrite a file:
{"action": "write", "file": "relative/path.py", "content": "full file contents"}

3. Request permission to run a shell command (REQUIRED after writing code):
{"action": "run", "command": "python app.py"}

4. Finish when ALL work is done AND the code runs successfully:
{"action": "done", "summary": "..."}

MANDATORY VERIFICATION LOOP:
- After writing code, you MUST "run" it.
- If run output shows errors, fix via "write" using error details, then "run" again.
- Only say "done" once code actually runs.

CRITICAL FORMATTING RULES FOR THE "write" ACTION:
- The "content" field MUST contain REAL newline characters, properly indented code.
- Python: 4-space indentation.
- NEVER write code as one long single-line string.
- NEVER use placeholders. ALWAYS provide COMPLETE final file content.

RULES:
- ALWAYS "read" existing files before updating them.
- Put all implementation and test code in workspace files using the write action.
- NEVER put source code in inline commands such as python -c, node -e, or PowerShell -Command.
- Do not chain commands with &&, &, ;, pipes, or redirection. The runner already starts in the workspace.
- Prefer running project test/build scripts, e.g. python -m pytest or npm test.
- Do not install packages automatically; report missing dependencies for the user to install.
- If purely conversational (no code), respond with:
{"action": "done", "summary": "<your answer>"}
"""

ASK_PROMPT = """You are myCodingAgent in ASK mode (like Copilot Chat).
Answer the user's question about their code / project clearly and concisely.
Reply with plain markdown text (no JSON, no actions).
"""

EDIT_PROMPT = """You are myCodingAgent in EDIT mode (like Copilot inline edits).
Make focused, minimal changes to the requested files. Respond ONLY with a
single JSON object using actions read / write / done (same schema as agent
mode). Do not run long servers.
"""


# ============================================================
# Workspace helpers
# ============================================================
def build_file_index(workspace=None):
    workspace = Path(workspace or WORKSPACE).resolve()
    entries = []
    for p in sorted(workspace.rglob("*")):
        if not p.is_file():
            continue
        if any(part in IGNORED_DIRS for part in p.parts):
            continue
        try:
            rel = str(p.relative_to(workspace))
            size = p.stat().st_size
            first_line = ""
            if size < 500_000:
                try:
                    first_line = (p.read_text(encoding="utf-8",
                                              errors="ignore")
                                  .splitlines()[0][:60])
                except Exception:
                    pass
            entries.append(f"- {rel} ({size}b) | {first_line}")
            if len(entries) >= MAX_INDEX_FILES:
                entries.append("- ... (index truncated)")
                break
        except OSError:
            continue
    return "\n".join(entries) if entries else "(workspace is empty)"


def safe_path(rel_path, workspace=None):
    workspace = Path(workspace or WORKSPACE).resolve()
    p = (workspace / rel_path).resolve()
    if workspace not in p.parents and p != workspace:
        raise ValueError(f"Path escapes workspace: {rel_path}")
    return p


# ============================================================
# Tools
# ============================================================
def tool_read(files, workspace=None):
    out = []
    for f in files[:5]:
        try:
            p = safe_path(f, workspace)
            if not p.exists():
                out.append(f"--- {f} ---\n(FILE NOT FOUND)")
                continue
            content = p.read_text(encoding="utf-8", errors="ignore")
            if len(content) > MAX_FILE_SIZE:
                content = content[:MAX_FILE_SIZE] + "\n... (truncated)"
            out.append(f"--- {f} ---\n{content}\n--- end of {f} ---")
        except Exception as e:
            out.append(f"--- {f} ---\n(ERROR: {e})")
    return "\n\n".join(out)


def validate_python_syntax(path):
    try:
        py_compile.compile(str(path), doraise=True)
        return None
    except Exception as e:
        return str(e)


def run_indent_fixer(path):
    if not INDENT_MAKER_AVAILABLE:
        return False, []
    try:
        _, changed, msgs = indent_maker.fix_file(path, apply_aggressive=True)
        return changed, msgs
    except Exception as e:
        return False, [f"indent_fixer error: {e}"]


def tool_write(path, content, workspace=None):
    workspace = Path(workspace or WORKSPACE).resolve()
    p = safe_path(path, workspace)
    if p.exists():
        backup_dir = workspace / ".mca_backups"
        backup_dir.mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup = backup_dir / f"{p.stem}_{ts}{p.suffix}"
        shutil.copy2(p, backup)
        result = f"OK {path}: updated (backup: {backup.name})"
    else:
        result = f"OK {path}: created"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")

    im_changed, im_msgs = run_indent_fixer(p)
    if im_changed:
        result += " (indent_fixer auto-fixed)"
    for m in im_msgs:
        print(f"   [fix] {m}")

    syntax_error = None
    if p.suffix.lower() == ".py":
        syntax_error = validate_python_syntax(p)
    return result, syntax_error


def tool_run(command, timeout=COMMAND_TIMEOUT, workspace=None):
    print(f"   run> {command}")
    blocked = ["rm -rf /", "format c:", "shutdown", "rmdir /s",
               "del /s", "remove-item", "drop database", "git reset --hard"]
    low = command.lower()
    for b in blocked:
        if b in low:
            return False, "BLOCKED: dangerous command refused."
    if re.search(r"(?:^|\s)(?:python(?:\d+(?:\.\d+)*)?|py)\s+-c(?:\s|$)",
                 low):
        return False, ("BLOCKED: inline Python is not allowed. Put the code in "
                       "a workspace .py file, then run that file or a test module.")
    if re.search(r"(?:^|\s)node\s+-e(?:\s|$)", low):
        return False, ("BLOCKED: inline JavaScript is not allowed. Put the code "
                       "in a workspace .js file, then run that file or a test script.")
    if re.search(r"(?:^|\s)powershell(?:\.exe)?\s+-(?:command|encodedcommand)\b",
                 low):
        return False, ("BLOCKED: inline PowerShell is not allowed. Put code in "
                       "a workspace script file and run the script explicitly.")
    if re.search(r"(?:^|\s)(?:cmd(?:\.exe)?\s+/c|pwsh(?:\.exe)?\s+-|"
                 r"bash\s+-c|sh\s+-c|ruby\s+-e|perl\s+-e)\b", low):
        return False, ("BLOCKED: inline command interpreters are not allowed. "
                       "Put code in a workspace file and run that file directly.")
    if re.search(r"(?:^|\s)(?:pip|pip\d+(?:\.\d+)?|python(?:\d+(?:\.\d+)*)?\s+-m\s+pip)\s+install\b",
                 low):
        return False, ("BLOCKED: automatic package installation is disabled. "
                       "List the dependency and let the user install it.")
    if re.search(r"&&|\|\||[|;<>]|(?<!\^)\s&\s", command):
        return False, ("BLOCKED: chained shell commands and redirection are not "
                       "allowed. The runner already uses the workspace as its "
                       "working directory; request one project command at a time.")
    try:
        proc = subprocess.run(command, shell=True,
                              cwd=str(Path(workspace or WORKSPACE).resolve()),
                              capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8",
                              errors="replace")
        stdout = (proc.stdout or "")[-4000:]
        stderr = (proc.stderr or "")[-4000:]
        out = (f"$ {command}\n[exit code: {proc.returncode}]\n"
               f"--- STDOUT ---\n{stdout or '(empty)'}\n"
               f"--- STDERR ---\n{stderr or '(empty)'}")
        return proc.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, (f"$ {command}\n[TIMEOUT after {timeout}s. "
                       "The command did not finish; verify it explicitly "
                       "before reporting success.]")
    except Exception as e:
        return False, f"$ {command}\n[ERROR launching process: {e}]"


# ============================================================
# JSON command parsing (robust)
# ============================================================
def parse_command(reply):
    try:
        return json.loads(reply.strip())
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", reply, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    depth, start, candidates, in_str, esc = 0, None, [], False, False
    for i, ch in enumerate(reply):
        if ch == '"' and not esc:
            in_str = not in_str
        esc = (ch == "\\" and in_str)
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(reply[start:i + 1])
    for cand in sorted(candidates, key=len, reverse=True):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None


# ============================================================
# Agent loop
# ============================================================
def run_agent(user_request, log=print, mode="agent", history=None,
              workspace=None, approval_callback=None):
    """Run the agent.

    mode: 'agent' | 'edit' | 'ask'
    history: optional ongoing conversation (Copilot-chat style);
             extended in place and reused.
    Returns a dict with status/reply/files/commands.
    """
    workspace = Path(workspace or WORKSPACE).resolve()
    if history is None:
        history = []

    im_status = "available" if INDENT_MAKER_AVAILABLE else "NOT FOUND"

    if not history:
        system_prompt = {"agent": AGENT_PROMPT,
                         "edit": EDIT_PROMPT,
                         "ask": ASK_PROMPT}.get(mode, AGENT_PROMPT)
        context = (f"[Agent instructions]\n{system_prompt}\n\n"
               f"# Workspace files:\n{build_file_index(workspace)}\n\n"
                   f"# Environment: Windows, Python {sys.version.split()[0]}. "
                   f"Use Windows-compatible commands.\n"
                   f"# indent_fixer is {im_status} (bundled) - written "
                   f"files are auto-checked and fixed for indentation.\n")
        history.append({"role": "user", "content": context})

    mode_rules = {
        "agent": "You may read, propose file writes, and run permitted project checks. File writes require human approval; shell commands are policy-checked and may run automatically only in a trusted VS Code workspace.",
        "edit": "You may read and propose file writes only. Do not run shell commands.",
        "ask": "Answer only. Do not use file or command tools.",
    }.get(mode, "Answer only; no tools are allowed.")
    active_prompt = {"agent": AGENT_PROMPT, "edit": EDIT_PROMPT,
                     "ask": ASK_PROMPT}.get(mode, ASK_PROMPT)
    history.append({"role": "user", "content":
                    f"[Current mode: {mode}]\n{active_prompt}\n"
                    f"Enforced mode rule: {mode_rules}\n"
                    f"# User request:\n{user_request}"})

    changed_files, executed = [], []
    max_steps = MAX_AGENT_STEPS if mode == "agent" else 12

    for turn in range(max_steps):
        log(f"\n[myCodingAgent step {turn + 1}/{max_steps}] ({mode} mode)")
        try:
            reply = ask_llm(history)
        except VerificationRequiredError as e:
            message = str(e)
            log("   HUMAN VERIFICATION REQUIRED")
            return {"status": "verification_required", "reply": message,
                    "files": changed_files, "commands": executed}
        except Exception as e:
            return {"status": "error", "reply": str(e),
                    "files": changed_files, "commands": executed}
        history.append({"role": "assistant", "content": reply})

        if mode == "ask":
            return {"status": "chat", "reply": reply,
                    "files": changed_files, "commands": executed}

        command = parse_command(reply)
        if command is None or not isinstance(command, dict) \
                or "action" not in command:
            if turn == 0:
                history.append({"role": "user", "content":
                                "Your last response was not valid action "
                                "JSON. Resend exactly ONE valid JSON object "
                                "with action read/write/run/done."})
                continue
            return {"status": "chat", "reply": reply,
                    "files": changed_files, "commands": executed}

        action = command["action"]

        if action == "read":
            files = command.get("files", [])
            content = tool_read(files, workspace)
            log(f"   read {files}")
            history.append({"role": "user",
                            "content": f"# File contents:\n{content}"})

        elif action == "write":
            if mode == "ask":
                return {"status": "permission_denied",
                        "reply": "ASK mode is read-only; no files were changed.",
                        "files": changed_files, "commands": executed}
            path = command.get("file", "")
            content = command.get("content", "")
            if not isinstance(content, str) or not path:
                history.append({"role": "user", "content":
                                "Invalid write action: provide a file path and string content."})
                continue
            try:
                target = safe_path(path, workspace)
            except (ValueError, OSError) as e:
                history.append({"role": "user", "content":
                                f"Write refused: {e}"})
                continue
            old_content = ""
            if target.exists() and target.is_file():
                old_content = target.read_text(encoding="utf-8", errors="replace")
            diff = "".join(difflib.unified_diff(
                old_content.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{path}" if target.exists() else "/dev/null",
                tofile=f"b/{path}"))
            approved = bool(approval_callback and approval_callback({
                "action": "write", "file": path, "diff": diff,
                "exists": target.exists()}))
            if not approved:
                return {"status": "approval_denied",
                        "reply": f"Write to {path} was not approved; no change was made.",
                        "files": changed_files, "commands": executed}
            result, syntax_error = tool_write(path, content, workspace)
            changed_files.append(path)
            log(f"   {result}")
            if syntax_error:
                history.append({"role": "user", "content":
                                f"# Syntax error in {path}\n{syntax_error}\n"
                                "Fix and rewrite the whole file."})
            else:
                history.append({"role": "user",
                                "content": f"# Result:\n{result}\n"
                                           "Now RUN it to verify."})

        elif action == "run":
            if mode != "agent":
                history.append({"role": "user", "content":
                                f"Command refused: {mode.upper()} mode cannot run shell commands. Use read/write/done only."})
                continue
            cmd = command.get("command", "")
            if not isinstance(cmd, str) or not cmd.strip():
                history.append({"role": "user", "content":
                                "Invalid run action: provide a non-empty command."})
                continue
            approved = bool(approval_callback and approval_callback({
                "action": "run", "command": cmd}))
            if not approved:
                return {"status": "approval_denied",
                        "reply": f"Command was not approved and was not run: {cmd}",
                        "files": changed_files, "commands": executed}
            ok, output = tool_run(cmd, workspace=workspace)
            executed.append(cmd)
            history.append({"role": "user",
                            "content": f"# Command output:\n{output}\n\n"
                                       "If the command was blocked, do not retry with inline code or shell chaining; write any needed code into a workspace file and run a supported project command. If output shows errors, fix them. "
                                       "If everything runs clean, reply with "
                                       "the done action."})

        elif action == "done":
            summary = command.get("summary", "(no summary)")
            log(f"   DONE: {summary}")
            return {"status": "done", "reply": summary,
                    "files": changed_files, "commands": executed}

        else:
            history.append({"role": "user", "content":
                            f"Unknown action '{action}'. Use "
                            "read/write/run/done."})

    return {"status": "max_steps", "reply": "Stopped step limit reached.",
            "files": changed_files, "commands": executed}


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) or "Say hello"
    result = run_agent(task)
    print("\n=== RESULT ===")
    print(result["status"], "-", result["reply"][:500])
