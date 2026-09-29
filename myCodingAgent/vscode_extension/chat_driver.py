"""
chat_driver.py - one-shot CLI bridge between the VS Code extension and
myCodingAgent. Prints a single JSON object to stdout.

Usage:
    python chat_driver.py --mode agent --task "build a todo app"
"""

import argparse
import io
import json
import os
import sys
from pathlib import Path

# UTF-8 safe console on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                      errors="replace")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# this file lives in myCodingAgent/vscode_extension/ -> add the parent
# (myCodingAgent/) so `import agent`, `llm`, `indent_fixer` resolve
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["agent", "edit", "ask"],
                        default="agent")
    parser.add_argument("--task", required=True)
    parser.add_argument("--agent-folder", required=True,
                        help="Folder containing agent.py and bundled modules")
    parser.add_argument("--history-file", default=None,
                        help="JSON file for persistent conversation memory")
    parser.add_argument("--approval-stdio", action="store_true",
                        help="request write/run approval over stdin/stdout")
    args = parser.parse_args()

    agent_folder = Path(args.agent_folder).resolve()
    if not (agent_folder / "agent.py").is_file():
        parser.error("--agent-folder must contain agent.py")
    sys.path.insert(0, str(agent_folder))
    from agent import run_agent  # noqa: E402

    history = []
    hist_path = Path(args.history_file) if args.history_file else None
    if hist_path and hist_path.exists():
        try:
            history = json.loads(hist_path.read_text(encoding="utf-8"))
        except Exception:
            history = []

    def approve(request):
        if not args.approval_stdio:
            return False
        print("MCA_APPROVAL:" + json.dumps(request, ensure_ascii=False),
              flush=True)
        answer = sys.stdin.readline().strip().lower()
        return answer in ("y", "yes", "approve", "approved")

    result = run_agent(args.task, mode=args.mode, history=history,
                       workspace=Path.cwd(), approval_callback=approve)

    if hist_path:
        # keep memory bounded
        try:
            hist_path.write_text(json.dumps(history[-40:], ensure_ascii=False),
                                 encoding="utf-8")
        except Exception:
            pass

    print("MCA_RESULT_START")
    print(json.dumps({
        "status": result.get("status"),
        "reply": (result.get("reply") or "")[:8000],
        "files": result.get("files", []),
        "commands": result.get("commands", []),
    }, ensure_ascii=False))
    print("MCA_RESULT_END")


if __name__ == "__main__":
    main()
