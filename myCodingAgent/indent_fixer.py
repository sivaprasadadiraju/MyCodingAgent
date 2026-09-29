"""
indent_fixer.py - Auto-fix indentation & formatting of code files.

Bundled inside myCodingAgent so the package is fully self-contained.

Usage:
    python indent_fixer.py                -> fixes all .py files in workspace
    python indent_fixer.py app.py utils.py -> fixes specific files
    python indent_fixer.py --check        -> only report, don't change

Also importable:
    from indent_fixer import fix_file, fix_all_python_files
"""

import sys
import time
import py_compile
from pathlib import Path

FIXABLE_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css",
                ".java", ".cs", ".cpp", ".c", ".go", ".rs", ".rb", ".json"}
BACKUP_DIR = Path(".indent_backups")
IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                ".agent_backups", ".indent_backups", ".mca_backups",
                ".idea", ".vscode"}

# Python keywords/statements that START a new indented block
BLOCK_OPENERS = (
    "if ", "if(", "elif ", "elif(", "else:", "else :",
    "for ", "for(", "while ", "while(",
    "def ", "class ", "try:", "try :", "except", "finally:",
    "with ", "match ", "case ",
)


# ============================================================
# Core cleaning (works for ALL languages)
# ============================================================
def basic_clean(content):
    """Literal \\n repair, tab normalization, trailing whitespace, blank-line trim."""
    fixes = []
    original = content

    # 1. Literal \n sequences with no real newlines -> real newlines
    if content.count("\\n") > 3 and content.count("\n") == 0:
        content = content.replace("\\r\\n", "\n").replace("\\n", "\n")
        fixes.append("literal \\n -> newlines")

    # 2. Literal
    if "\    " in content and "    " not in content.replace("\    ", ""):
        content = content.replace("\    ", "    ")
        fixes.append("literal \     -> spaces")

    # 3. Real tabs -> 4 spaces
    if "    " in content:
        content = content.expandtabs(4)
        fixes.append("tabs -> 4 spaces")

    # 4. Trailing whitespace per line
    lines = content.split("\n")
    stripped = [ln.rstrip() for ln in lines]
    if stripped != lines:
        fixes.append("trailing whitespace removed")
    lines = stripped

    # 5. Trim leading/trailing blank lines, single trailing newline
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    content = "\n".join(lines) + "\n"

    # 6. Normalize CRLF -> LF
    if "\r\n" in content:
        content = content.replace("\r\n", "\n")
        fixes.append("CRLF -> LF")

    if content != original:
        return content, fixes or ["whitespace normalized"]
    return content, []


# ============================================================
# Python-specific reindentation
# ============================================================
def reindent_python(content):
    """
    Rebuild Python indentation from logical structure.
    Only used as a LAST RESORT (when py_compile fails), because
    guessing can break code that was actually fine.
    """
    fixes = []
    raw_lines = content.split("\n")
    out = []
    indent = 0

    for raw in raw_lines:
        line = raw.strip()

        if not line:
            out.append("")
            continue

        # Comment-only line: keep at current indent
        if line.startswith("#"):
            out.append("    " * indent + line)
            continue

        # Line reduces indent BEFORE executing (else/elif/except/finally)
        lowered = line.lstrip()
        dedent_first = (lowered.startswith("else") and lowered.endswith(":")) or \
                       lowered.startswith("elif ") or \
                       lowered.startswith("except") or \
                       lowered.startswith("finally:")

        if dedent_first and indent > 0:
            indent -= 1

        out.append("    " * indent + line)

        # opens a block -> next line is indented
        if lowered.endswith(":") and not lowered.startswith(("print(", "#")):
            indent += 1

        if indent < 0:
            indent = 0

    result = "\n".join(out)
    if result != content:
        fixes.append("python blocks reindented")
    if not result.endswith("\n"):
        result += "\n"
    return result, fixes


def try_fix_python(path, content, apply_aggressive):
    """Return (fixed_content, fixes_list)."""
    fixed, fixes = basic_clean(content)

    # Already valid? Done.
    tmp = path.with_suffix(".tmp_check")
    tmp.write_text(fixed, encoding="utf-8")
    error = None
    try:
        py_compile.compile(str(tmp), doraise=True)
    except Exception as e:
        error = str(e)
    finally:
        tmp.unlink(missing_ok=True)

    if error is None:
        return fixed, fixes

    if not apply_aggressive:
        return fixed, fixes + [f"still has syntax error: {error.splitlines()[0]}"]

    # Aggressive: rebuild indentation
    fixed2, fixes2 = reindent_python(fixed)
    tmp.write_text(fixed2, encoding="utf-8")
    try:
        py_compile.compile(str(tmp), doraise=True)
        fixes2.append("now compiles OK")
        error2 = None
    except Exception as e:
        error2 = e
    finally:
        tmp.unlink(missing_ok=True)

    if error2 is None:
        return fixed2, fixes2

    # Reindent failed — keep the safer version, report error
    return fixed, fixes + [f"could not auto-fix: {str(error2).splitlines()[0]}"]


# ============================================================
# Public API
# ============================================================
def fix_file(file_path, apply_aggressive=True, dry_run=False):
    """Fix one file. Returns (path, changed: bool, messages: list)."""
    p = Path(file_path)
    if not p.exists():
        return p, False, ["file not found"]
    if p.suffix.lower() not in FIXABLE_EXTS:
        return p, False, ["not a fixable code file"]

    try:
        content = p.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return p, False, [f"read error: {e}"]

    if p.suffix.lower() == ".py":
        fixed, msgs = try_fix_python(p, content, apply_aggressive)
    else:
        fixed, msgs = basic_clean(content)

    changed = (fixed != content)

    if changed and not dry_run:
        BACKUP_DIR.mkdir(exist_ok=True)
        backup = BACKUP_DIR / f"{p.stem}_{time.strftime('%Y%m%d_%H%M%S')}{p.suffix}"
        backup.write_text(content, encoding="utf-8")
        p.write_text(fixed, encoding="utf-8")

    return p, changed, msgs


def fix_all_python_files(root=".", dry_run=False):
    results = []
    for p in sorted(Path(root).rglob("*")):
        if not p.is_file() or p.suffix.lower() != ".py":
            continue
        if any(part in IGNORED_DIRS for part in p.parts):
            continue
        if p.name.startswith("indent_fixer") or p.name.startswith("indent_maker"):
            continue
        results.append(fix_file(p, dry_run=dry_run))
    return results


def print_report(results):
    changed_count = 0
    for path, changed, msgs in results:
        status = "FIXED" if changed else "OK    "
        print(f"  {status}  {path}")
        for m in msgs:
            print(f"            - {m}")
        if changed:
            changed_count += 1
    print(f"\n  {changed_count}/{len(results)} files changed.")


# ============================================================
# CLI
# ============================================================
def main():
    args = sys.argv[1:]
    dry_run = "--check" in args
    args = [a for a in args if not a.startswith("--")]

    print("=" * 55)
    print(" indent_fixer - auto-fix code indentation")
    print("=" * 55)

    if args:
        results = [fix_file(a, dry_run=dry_run) for a in args]
    else:
        results = fix_all_python_files(".", dry_run=dry_run)

    print_report(results)

    if dry_run:
        print("\n  (dry run - no files modified. Run without --check to fix.)")


if __name__ == "__main__":
    main()
