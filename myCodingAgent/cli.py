"""
myCodingAgent/cli.py - Interactive Copilot-style CLI chat.

Usage:
    python cli.py                            -> interactive chat (task mode default)
    python cli.py "build a todo flask app"   -> one-shot agent task
    python cli.py --ask "explain app.py"     -> ask-only (no file changes)
    python cli.py --edit "rename foo in app.py"
"""

import io
import os
import re
import sys
from pathlib import Path

# --- Windows console UTF-8 safety (emoji / unicode output) ---
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import run_agent, INDENT_MAKER_AVAILABLE  # noqa: E402

HELP = """
myCodingAgent CLI - Copilot-style coding chat
----------------------------------------------
Modes:
  /agent <task>   full autonomous mode (create/run/fix) [default]
    /task <task>    alias for /agent
    /edit <task>    edit files only, no commands (asks before writing)
  /ask <question> answer only, no file changes

Other commands:
  /files          list workspace files the agent can see
  /new            start a fresh conversation (clears memory)
  /clear          clear the terminal screen
  /help           show this help
  /exit or /quit  leave

Tips:
  - @filename reads that file into context first, e.g. "@app.py add a route"
    - Plain text is treated as a TASK; writes and shell commands require approval
"""


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def list_workspace():
    root = Path(".")
    ignored = {".git", "node_modules", "__pycache__", ".venv", "venv",
               ".mca_backups", ".agent_backups", ".indent_backups",
               ".idea", ".vscode", "myCodingAgent"}
    entries = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in ignored for part in p.parts):
            continue
        try:
            entries.append(f"  {p} ({p.stat().st_size}b)")
        except OSError:
            pass
        if len(entries) >= 100:
            entries.append("  ... (truncated)")
            break
    print("\n".join(entries) if entries else "  (workspace is empty)")


def extract_at_files(text):
    """Return (cleaned_text, [files]) - pulls @filename tokens out of input."""
    files = re.findall(r"@([\w./\\-]+)", text)
    cleaned = re.sub(r"@[\w./\\-]+", "", text).strip()
    return cleaned, files


def safe_print(*args, **kwargs):
    """Print that never crashes on unencodable characters."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        msg = " ".join(str(a) for a in args)
        print(msg.encode("ascii", errors="replace").decode("ascii"), **kwargs)


def handle_result(result):
    if result["status"] == "chat":
        safe_print("\nagent>", result["reply"])
    elif result["status"] == "done":
        safe_print("\nagent> done:", result["reply"])
        safe_print(" files touched:", result["files"] or "none")
        if result["commands"]:
            safe_print(" commands run:", result["commands"])
    else:
        safe_print("\nagent>", result["reply"])


def terminal_approval(request):
    """Require an explicit local confirmation for every write or command."""
    if request["action"] == "run":
        safe_print("\nCommand requested:", request["command"])
    else:
        safe_print("\nProposed file change:", request["file"])
        diff_lines = (request.get("diff") or "").splitlines()
        safe_print("\n".join(diff_lines[:60]) or "(empty file)")
        if len(diff_lines) > 60:
            safe_print("... diff truncated for approval prompt ...")
    try:
        return input("Approve? [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def main():
    args = sys.argv[1:]
    mode = "agent"
    if args and args[0] in ("/ask", "/edit", "/task", "/agent"):
        mode = {"/ask": "ask", "/edit": "edit",
                "/task": "agent", "/agent": "agent"}[args.pop(0)]
    if "--ask" in args:
        mode = "ask"
        args.remove("--ask")
    elif "--edit" in args:
        mode = "edit"
        args.remove("--edit")

    safe_print("=" * 55)
    safe_print(" myCodingAgent - autonomous coding agent (standalone)")
    safe_print("=" * 55)
    safe_print(f" indent_fixer: {'available' if INDENT_MAKER_AVAILABLE else 'missing'}")
    safe_print(HELP)

    history = []  # ongoing conversation memory across turns

    # ---- One-shot task from command line ----
    if args:
        task = " ".join(args)
        task, at_files = extract_at_files(task)
        if at_files:
            run_agent("read " + ", ".join(at_files),
                      mode="edit", history=history, workspace=Path.cwd(),
                      approval_callback=terminal_approval)
        result = run_agent(task, mode=mode, history=history,
                           workspace=Path.cwd(),
                           approval_callback=terminal_approval)
        safe_print("\n=== RESULT ===")
        safe_print(result["status"], "-", result["reply"][:1000])
        return

    # ---- Interactive loop ----
    current_mode = mode
    while True:
        try:
            line = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            safe_print("\nbye")
            break
        if not line:
            continue

        if line.lower() in ("/exit", "/quit", "exit", "quit", "q"):
            safe_print("bye")
            break
        if line in ("/help", "help", "?"):
            safe_print(HELP)
            continue
        if line == "/files":
            list_workspace()
            continue
        if line == "/new":
            history.clear()
            safe_print("  (conversation cleared)")
            continue
        if line == "/clear":
            clear_screen()
            continue

        # mode switching
        if line.startswith("/agent") or line.startswith("/task"):
            current_mode = "agent"
            command_len = 6 if line.startswith("/agent") else 5
            line = line[command_len:].strip()
            if not line:
                safe_print("  mode -> TASK/agent (default). Enter your task.")
                continue
        elif line.startswith("/edit"):
            current_mode = "edit"
            line = line[5:].strip()
            if not line:
                safe_print("  mode -> EDIT. Enter your edit instruction.")
                continue
        elif line.startswith("/ask"):
            current_mode = "ask"
            line = line[4:].strip()
            if not line:
                safe_print("  mode -> ASK. Enter your question.")
                continue

        # @file handling: read referenced files into context first
        line, at_files = extract_at_files(line)
        if at_files:
            run_agent("read " + ", ".join(at_files),
                      mode="edit", history=history, workspace=Path.cwd(),
                      approval_callback=terminal_approval)

        if not line:
            continue

        result = run_agent(line, mode=current_mode, history=history,
                   workspace=Path.cwd(),
                   approval_callback=terminal_approval)
        handle_result(result)


if __name__ == "__main__":
    main()
