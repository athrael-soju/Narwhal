"""Check source Markdown and generated wiki links locally.

Canonical Narwhal URLs resolve against the checkout or rendered wiki.
Checks cover Markdown and HTML targets and skip fenced code and third-party URLs.
"""

from __future__ import annotations

import argparse
import html
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_LINK = re.compile(r"<(?:a|img)\b[^>]*?\b(?:href|src)\s*=\s*[\"']([^\"']+)[\"']", re.I)
ROOT = Path(__file__).resolve().parents[1]
CANONICAL = "/athrael-soju/Narwhal/"


def tracked_markdown() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [ROOT / line for line in out.stdout.splitlines() if (ROOT / line).is_file()]


def prose(text: str) -> str:
    """Remove fenced code before interpreting Markdown structure."""
    lines = []
    fence = ""
    for line in text.splitlines():
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match:
            marker = match[1]
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ""
            lines.append("")
        elif not fence:
            lines.append(line)
    return "\n".join(lines)


def anchors(text: str) -> set[str]:
    """Collect GitHub-style headings and explicit HTML anchors."""
    result = set(re.findall(r'<[^>]+(?:id|name)=["\']([^"\']+)["\']', text))
    used: set[str] = set()
    previous = ""
    for line in text.splitlines():
        match = re.match(r"^ {0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$", line)
        title = match[1] if match else ""
        if previous.strip() and re.fullmatch(r" {0,3}(?:=+|-+)\s*", line):
            title = previous.strip()
        previous = line
        if not title:
            continue
        title = LINK.sub(lambda m: m[1], title)
        title = html.unescape(re.sub(r"<[^>]+>", "", title)).lower()
        slug = "".join(
            c for c in title if c in "_-" or not unicodedata.category(c).startswith(("P", "S", "C"))
        ).replace(" ", "-")
        candidate, number = slug, 0
        while candidate in used:
            number += 1
            candidate = f"{slug}-{number}"
        used.add(candidate)
        result.add(candidate)
    return result


def local_target(
    md: Path, target: str, root: Path, wiki_root: Path | None = None
) -> tuple[Path, str] | None:
    """Resolve local and canonical repository links without network access."""
    url = urlsplit(target)
    path = unquote(url.path)
    if url.scheme or url.netloc:
        if url.netloc.lower() != "github.com" or not path.startswith(CANONICAL):
            return None
        relative = path[len(CANONICAL) :]
        if relative.startswith(("blob/main/", "tree/main/")):
            path = relative.split("/", 2)[2]
            destination = root / path
        elif relative == "wiki" or relative.startswith("wiki/"):
            page = relative.removeprefix("wiki").strip("/") or "Home"
            destination = (wiki_root or root / "docs") / (
                page if page.endswith(".md") else page + ".md"
            )
        else:
            return None
    else:
        destination = md.parent / path if path else md
        if wiki_root is not None and path and not destination.suffix:
            destination = destination.with_suffix(".md")
    return destination, unquote(url.fragment)


def dangling(files: list[Path], root: Path = ROOT, wiki_root: Path | None = None) -> list[str]:
    bad = []
    cache: dict[Path, set[str]] = {}
    for md in files:
        text = prose(md.read_text())
        targets = [target for _, target in LINK.findall(text)] + HTML_LINK.findall(text)
        for target in targets:
            resolved = local_target(md, target, root, wiki_root)
            if resolved is None:
                continue
            path, fragment = resolved
            url = urlsplit(target)
            if (
                wiki_root is not None
                and not (url.scheme or url.netloc)
                and not path.resolve().is_relative_to(wiki_root.resolve())
            ):
                bad.append(f"{md}: {target} (outside generated wiki)")
                continue
            if not path.exists():
                bad.append(f"{md}: {target} (missing path)")
            elif fragment and path.suffix.lower() == ".md":
                if path not in cache:
                    cache[path] = anchors(prose(path.read_text()))
                if fragment not in cache[path]:
                    bad.append(f"{md}: {target} (missing anchor)")
    return bad


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wiki-root", type=Path, help="validate an already rendered wiki directory"
    )
    args = parser.parse_args()
    if args.wiki_root is not None:
        if not args.wiki_root.is_dir():
            parser.error(f"wiki directory does not exist: {args.wiki_root}")
        bad = dangling(sorted(args.wiki_root.glob("*.md")), wiki_root=args.wiki_root)
        generated_status = 0
    else:
        bad = dangling(tracked_markdown())
        generated_status = subprocess.run(
            ["bash", str(ROOT / "tools/publish_wiki.sh"), "--check"], cwd=ROOT, check=False
        ).returncode
    for line in bad:
        print(line)
    if bad:
        print(f"{len(bad)} dangling link(s)", file=sys.stderr)
    return 1 if bad or generated_status else 0


if __name__ == "__main__":
    raise SystemExit(main())
