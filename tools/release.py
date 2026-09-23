"""Validate release identity and publish verified distributions without overwriting assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY = "athrael-soju/Narwhal"
RELEASE_BRANCH = "release-please--branches--main"
INITIAL_RELEASE_BRANCH = "release/v0.1.0"
VERSION = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
HEADING = re.compile(
    rf"^## (?:v?({VERSION})|\[v?({VERSION})\]\([^\n]+\))(?: \(\d{{4}}-\d{{2}}-\d{{2}}\))?$", re.M
)


def command(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def api(path: str, *args: str):
    endpoint = f"repos/{REPOSITORY}"
    if path:
        endpoint += f"/{path}"
    result = command("gh", "api", endpoint, *args)
    return json.loads(result) if result else None


def one(pattern: str, text: str, name: str) -> str:
    values = re.findall(pattern, text, flags=re.M)
    if len(values) != 1:
        raise ValueError(f"Expected one {name}, found {len(values)}")
    return values[0]


def version(root: Path) -> str:
    value = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(VERSION, value):
        raise ValueError("Release versions must use MAJOR.MINOR.PATCH")
    citation = one(
        r'^version: [\'"]?([^\s\'"\n]+)[\'"]?$',
        (root / "CITATION.cff").read_text(),
        "citation version",
    )
    if citation != value:
        raise ValueError("Package and citation versions disagree")
    return value


def notes(root: Path, expected: str) -> str:
    text = (root / "CHANGELOG.md").read_text()
    matches = [m for m in HEADING.finditer(text) if (m[1] or m[2]) == expected]
    if len(matches) != 1:
        raise ValueError(f"Expected one finalized changelog section for {expected}")
    match = matches[0]
    first = re.search(r"^## .+$", text, flags=re.M)
    if first is None or first.start() != match.start():
        raise ValueError("Release must be the first changelog section")
    body = re.split(r"^## ", text[match.end() :], maxsplit=1, flags=re.M)[0].strip()
    if not body:
        raise ValueError("Release notes are empty")
    return body


def validate(root: Path, tag: str | None = None) -> str:
    value = version(root)
    manifest = json.loads((root / ".release-please-manifest.json").read_text())
    if manifest.get(".") != value:
        raise ValueError("Release manifest and package version disagree")
    if tag is not None and tag != f"v{value}":
        raise ValueError("Release tag and package version disagree")
    notes(root, value)
    return value


def release_pr(pr: dict, sha: str, value: str) -> bool:
    labels = {label["name"] for label in pr["labels"]}
    branch = pr["head"]["ref"]
    return (
        bool(pr.get("merged_at"))
        and pr["merge_commit_sha"] == sha
        and pr["base"]["ref"] == "main"
        and (branch == RELEASE_BRANCH or (value == "0.1.0" and branch == INITIAL_RELEASE_BRANCH))
        and pr["head"]["repo"] is not None
        and pr["head"]["repo"]["full_name"] == REPOSITORY
        and bool(labels & {"autorelease: pending", "autorelease: tagged"})
    )


def candidate(root: Path) -> tuple[str, int | None] | None:
    sha = command("git", "rev-parse", "HEAD")
    command("git", "diff", "--exit-code", "HEAD")
    command("git", "merge-base", "--is-ancestor", sha, "origin/main")
    value = version(root)
    prs = [pr for pr in api(f"commits/{sha}/pulls") if release_pr(pr, sha, value)]
    if not prs:
        # A history-clean first release has no PR-associated root commit.
        if (
            value == "0.1.0"
            and command("git", "rev-parse", "origin/main") == sha
            and len(command("git", "rev-list", "--parents", "-n", "1", "HEAD").split()) == 1
        ):
            return validate(root), None
        return None
    if len(prs) != 1:
        raise ValueError("Multiple release PRs identify this commit")
    return validate(root), prs[0]["number"]


def asset_matches(asset: dict, path: Path) -> bool:
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    digest = asset.get("digest")
    if digest:
        return digest == f"sha256:{expected}"
    data = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{REPOSITORY}/releases/assets/{asset['id']}",
            "-H",
            "Accept: application/octet-stream",
        ],
        check=True,
        capture_output=True,
    ).stdout
    return hashlib.sha256(data).hexdigest() == expected


def verify_assets(assets: list[dict], files: dict[str, Path]) -> set[str]:
    existing = set()
    for asset in assets:
        name = asset["name"]
        if name in existing or name not in files or not asset_matches(asset, files[name]):
            raise ValueError(f"Existing release asset differs: {name}")
        existing.add(name)
    return existing


def pypi_status(root: Path, dist: Path) -> str:
    """Return whether PyPI has the exact wheel and source archive for this release."""
    value = validate(root)
    files = {path.name: path for path in dist.iterdir() if path.is_file()}
    expected = {f"narwhal_inference-{value}-py3-none-any.whl", f"narwhal_inference-{value}.tar.gz"}
    if set(files) != expected:
        raise ValueError("Expected exactly the versioned wheel and source archive")
    url = f"https://pypi.org/pypi/narwhal-inference/{value}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            release = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return "missing"
        raise
    uploaded = {file["filename"]: file for file in release["urls"]}
    if set(uploaded) != expected or any(
        uploaded[name]["digests"]["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in files.items()
    ):
        raise ValueError(f"PyPI version {value} contains different distribution files")
    return "matching"


def release_by_tag(tag: str) -> dict | None:
    pages = json.loads(
        command("gh", "api", "--paginate", "--slurp", f"repos/{REPOSITORY}/releases")
    )
    found = [release for page in pages for release in page if release["tag_name"] == tag]
    if len(found) > 1:
        raise ValueError("Duplicate release tag")
    return found[0] if found else None


def publish(root: Path, dist: Path) -> None:
    if os.environ.get("NARWHAL_RELEASES_ENABLED") != "true":
        raise ValueError("Release publication is disabled")
    if api("")["private"]:
        raise ValueError("Public releases require the launch cutover first")
    selected = candidate(root)
    if selected is None:
        raise ValueError("This commit is not a merged release PR")
    value, number = selected
    tag = f"v{value}"
    sha = command("git", "rev-parse", "HEAD")
    files = {p.name: p for p in dist.iterdir() if p.is_file()}
    expected = {f"narwhal_inference-{value}-py3-none-any.whl", f"narwhal_inference-{value}.tar.gz"}
    if set(files) != expected:
        raise ValueError("Expected exactly the versioned wheel and source archive")
    body = notes(root, value)
    # Verify an existing tag points to this commit before publishing.
    refs = command("git", "ls-remote", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    if refs:
        resolved = dict(line.split()[::-1] for line in refs.splitlines())
        target = resolved.get(f"refs/tags/{tag}^{{}}", resolved.get(f"refs/tags/{tag}"))
        if target != sha:
            raise ValueError("Existing tag points to a different commit")
    else:
        api("git/refs", "--method", "POST", "-f", f"ref=refs/tags/{tag}", "-f", f"sha={sha}")
    release = release_by_tag(tag)
    if release is None:
        release = api(
            "releases",
            "--method",
            "POST",
            "-f",
            f"tag_name={tag}",
            "-f",
            f"target_commitish={sha}",
            "-f",
            f"name=Narwhal {tag}",
            "-f",
            f"body={body}",
            "-F",
            "draft=false",
        )
    if release["body"].strip() != body:
        raise ValueError("Existing release notes differ from the reviewed changelog")
    existing = verify_assets(release["assets"], files)
    missing = sorted(set(files) - existing)
    for name in missing:
        command("gh", "release", "upload", tag, str(files[name]), "--repo", REPOSITORY)
    release = api(f"releases/{release['id']}")
    if verify_assets(release["assets"], files) != set(files):
        raise ValueError("Release assets are incomplete")
    if release["draft"]:
        raise ValueError("Release is still a draft")
    if number is not None:
        command(
            "gh",
            "pr",
            "edit",
            str(number),
            "--repo",
            REPOSITORY,
            "--remove-label",
            "autorelease: pending",
            "--add-label",
            "autorelease: tagged",
        )
    print(f"Published {tag} at {sha}")


def check_title(title: str) -> None:
    if not re.fullmatch(
        r"(?:feat|fix|perf|docs|ci|build|test|refactor|chore|revert)(?:\([^()\n]+\))?!?: \S[^\n]*",
        title,
    ):
        raise ValueError(
            "PR title must use a conventional prefix, for example: fix: preserve queued requests"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "candidate", "publish", "pypi-status", "title"])
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--tag")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    root = Path.cwd()
    try:
        if args.action == "title":
            check_title(os.environ["PR_TITLE"])
        elif args.action == "check":
            value = validate(root, args.tag) if args.release or args.tag else version(root)
            print(f"Version metadata agrees at {value}")
        elif args.action == "candidate":
            selected = candidate(root)
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                output.write(f"release={'true' if selected else 'false'}\n")
                if selected:
                    output.write(f"version={selected[0]}\n")
        elif args.action == "pypi-status":
            status = pypi_status(root, args.dist)
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                output.write(f"status={status}\n")
            print(f"PyPI release status: {status}")
        else:
            publish(root, args.dist)
    except (ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
