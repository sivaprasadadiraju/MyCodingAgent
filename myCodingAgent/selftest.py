"""Deterministic offline self-test for myCodingAgent."""
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path

HERE = Path(__file__).parent


def main():
    print("myCodingAgent self-test")
    print("=" * 40)
    ok = True

    # 1. Python version
    v = sys.version_info
    if v >= (3, 10):
        print(f"[OK] Python {v.major}.{v.minor}.{v.micro}")
    else:
        print(f"[FAIL] Python {v.major}.{v.minor} - requires 3.10+")
        ok = False

    # 2. Core imports
    sys.path.insert(0, str(HERE.parent))
    try:
        from myCodingAgent import agent
        from myCodingAgent import llm
        from myCodingAgent.llm import VerificationRequiredError
        print("[OK] package imports")
    except Exception as e:
        print(f"[FAIL] core imports: {e}")
        return 1

    # 3. Indent fixer (optional but recommended)
    if getattr(agent, "INDENT_MAKER_AVAILABLE", False):
        print("[OK] indent_fixer available")
    else:
        print("[WARN] indent_fixer not available (auto indent repair disabled)")

    # 4. Inline-code shell commands are never executed.
    for command in (
        "python -c \"print('inline')\"",
        "cd 'Web Report' && python -c \"print('inline')\"",
        "node -e \"console.log('inline')\"",
    ):
        with patch.object(agent.subprocess, "run") as run_process:
            ok_command, output = agent.tool_run(command)
            if ok_command or "BLOCKED" not in output or run_process.called:
                print(f"[FAIL] unsafe inline command was not blocked: {command}")
                ok = False
    if ok:
        print("[OK] inline code and chained commands are blocked")

    # 5. Agent permissions and verification handling (no network or shell)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        approvals = []
        replies = iter([
            '{"action":"write","file":"hello.py","content":"print(\'ok\')\\n"}',
            '{"action":"done","summary":"created"}',
        ])
        original_ask = agent.ask_llm
        try:
            captured = {}

            class FakeResponse:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return {"choices": [{"message": {"content": "ok"}}]}

                def close(self):
                    pass

            def fake_post(url, **kwargs):
                captured["url"] = url
                captured.update(kwargs)
                return FakeResponse()

            with patch.dict("os.environ", {
                "MYCODINGAGENT_LLM_BASE_URL": "http://localhost:11434/v1/",
                "MYCODINGAGENT_LLM_MODEL": "test-model",
                "MYCODINGAGENT_LLM_API_KEY": "test-only-key",
            }), patch.object(llm.session, "post", side_effect=fake_post):
                reply = llm.ask_llm([{"role": "user", "content": "hello"}])
            if (reply == "ok"
                    and captured["url"] == "http://localhost:11434/v1/chat/completions"
                    and captured["headers"]["authorization"] == "Bearer test-only-key"):
                print("[OK] alternate OpenAI-compatible provider works without network")
            else:
                print(f"[FAIL] alternate provider selection: {captured}")
                ok = False

            agent.ask_llm = lambda _history: next(replies)
            result = agent.run_agent(
                "create hello.py", mode="agent", history=[], workspace=root,
                approval_callback=lambda request: approvals.append(request) or True,
                log=lambda *_args, **_kwargs: None)
            if result["status"] == "done" and (root / "hello.py").exists():
                print("[OK] workspace-scoped write requires approval and succeeds")
            else:
                print(f"[FAIL] approved write: {result}")
                ok = False

            replies = iter(['{"action":"write","file":"denied.txt","content":"no"}'])
            result = agent.run_agent(
                "write a file", mode="agent", history=[], workspace=root,
                approval_callback=lambda _request: False,
                log=lambda *_args, **_kwargs: None)
            if result["status"] == "approval_denied" and not (root / "denied.txt").exists():
                print("[OK] denied writes are not applied")
            else:
                print(f"[FAIL] denied write: {result}")
                ok = False

            replies = iter(['{"action":"run","command":"python hello.py"}',
                            '{"action":"done","summary":"safe"}'])
            result = agent.run_agent(
                "do not run commands in edit mode", mode="edit", history=[],
                workspace=root, approval_callback=lambda _request: True,
                log=lambda *_args, **_kwargs: None)
            if result["status"] == "done" and not result["commands"]:
                print("[OK] edit mode blocks shell commands")
            else:
                print(f"[FAIL] edit mode command guard: {result}")
                ok = False

            def needs_human(_history):
                raise VerificationRequiredError("Complete verification manually")

            agent.ask_llm = needs_human
            result = agent.run_agent("test checkpoint", history=[], workspace=root,
                                     log=lambda *_args, **_kwargs: None)
            if result["status"] == "verification_required":
                print("[OK] human verification is surfaced as a distinct status")
            else:
                print(f"[FAIL] verification handling: {result}")
                ok = False
        finally:
            agent.ask_llm = original_ask

        try:
            agent.safe_path("../outside.txt", root)
            print("[FAIL] workspace traversal was not blocked")
            ok = False
        except ValueError:
            print("[OK] paths cannot escape the workspace")

    print("=" * 40)
    print("SELF-TEST PASSED" if ok else "SELF-TEST FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
