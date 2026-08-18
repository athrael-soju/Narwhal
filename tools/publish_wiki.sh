#!/usr/bin/env bash
# Mirror docs/ into the repository's GitHub wiki.
#
# The wiki is a mirror, never the source: pages live in docs/, reviewed and
# versioned with the code. GitHub only materializes the wiki repository once
# a first page exists, so on a fresh repository create any page through the
# web UI, then run this.
set -euo pipefail

repo_url=$(git remote get-url origin)
# WIKI_URL overrides the origin-derived target, for callers whose origin
# is not the repository the wiki belongs to (the mirror workflow).
wiki_url="${WIKI_URL:-${repo_url%.git}.wiki.git}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

if ! git clone --quiet --depth 1 "$wiki_url" "$tmp"; then
  echo "could not clone $wiki_url" >&2
  echo "create the wiki's first page through the web UI, then rerun" >&2
  exit 1
fi

# A true mirror: pages removed from docs/ leave the wiki too.
for page in "$tmp"/*.md; do
  [ -f "docs/$(basename "$page")" ] || git -C "$tmp" rm -q "$(basename "$page")"
done
cp docs/*.md "$tmp"/
# Pages may embed repository assets by relative path; the wiki repo has
# no assets tree, so referenced images travel with the mirror and the
# paths flatten to match.
if ls assets/architectures/*.svg > /dev/null 2>&1; then
  cp assets/architectures/*.svg "$tmp"/
fi
if ls assets/*.svg > /dev/null 2>&1; then
  cp assets/*.svg "$tmp"/
fi
sed -i 's|\.\./assets/architectures/||g; s|\.\./assets/||g' "$tmp"/*.md
# The repository renders [x](Page.md); the wiki resolves that to the raw
# file and wants [x](Page). Strip the extension from bare page links only,
# leaving URLs and pathed links alone.
sed -i -E 's/\]\(([A-Za-z0-9_-]+)\.md\)/](\1)/g' "$tmp"/*.md
cd "$tmp"
git add -A
if git diff --cached --quiet; then
  echo "wiki already matches docs/"
  exit 0
fi
git commit --quiet -m "Mirror docs/ at $(git -C "$OLDPWD" rev-parse --short HEAD)"
git push --quiet
echo "published $(ls *.md | wc -l) pages to the wiki"
