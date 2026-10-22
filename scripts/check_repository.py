"""Check source boundaries and local Markdown file links without opening user data."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPOSITORY = Path(__file__).resolve().parents[1]
PRIVATE_DIRECTORIES = {
    "data",
    "backups",
    "exports",
    "logs",
    "credentials",
    "secrets",
    ".local",
    ".codex",
    ".claude",
}
PRIVATE_SUFFIXES = {".pem", ".key", ".sqlite", ".sqlite3", ".db", ".db-wal", ".db-shm", ".log"}
INLINE_LINK = re.compile(r"!?\[[^\]\n]*\]\((<[^>\n]+>|[^\s)]+)(?:\s+\"[^\"]*\")?\)")


def source_files(root: Path) -> list[Path]:
    """Include forced tracked files even when .gitignore would otherwise hide them."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return sorted({Path(name.decode("utf-8")) for name in result.stdout.split(b"\0") if name})


def path_problem(root: Path, relative: Path) -> str | None:
    if relative.is_absolute() or ".." in relative.parts:
        return "path escapes repository"
    path = root / relative
    if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root):
        return "source symlinks require an explicit reviewed policy"
    if not path.is_file():
        return "source is missing or not a regular file"
    if PRIVATE_DIRECTORIES.intersection(relative.parts):
        return "runtime data or credential directory cannot be committed"
    if relative.suffix.lower() in PRIVATE_SUFFIXES:
        return "runtime data or credential file cannot be committed"
    if (
        relative.name == ".env" or relative.name.startswith(".env.")
    ) and relative.name != ".env.example":
        return "only the placeholder .env.example is permitted"
    return None


def markdown_problems(root: Path, relative: Path) -> list[str]:
    """Check inline file links, not remote URLs or heading anchors."""
    text = (root / relative).read_text(encoding="utf-8")
    problems = []
    fenced = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        for match in INLINE_LINK.finditer(line):
            destination = match.group(1).strip("<>")
            parts = urlsplit(destination)
            if parts.scheme or parts.netloc or not parts.path:
                continue
            target = (root / relative.parent / unquote(parts.path)).resolve()
            if not target.is_relative_to(root.resolve()):
                problems.append(f"{relative}:{line_number}: link escapes repository")
            elif not target.exists():
                problems.append(f"{relative}:{line_number}: missing linked file: {parts.path}")
    return problems


def check_repository(root: Path) -> list[str]:
    problems = []
    for relative in source_files(root):
        problem = path_problem(root, relative)
        if problem:
            problems.append(f"{relative}: {problem}")
        elif relative.suffix == ".md":
            problems.extend(markdown_problems(root, relative))
    return problems


def main() -> int:
    try:
        problems = check_repository(REPOSITORY)
    except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError):
        print("Repository check could not complete; refusing to pass.")
        return 1
    for problem in problems:
        print(problem)
    if not problems:
        print("Repository paths and inline local document links passed.")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
