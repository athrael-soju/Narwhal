#!/usr/bin/env bash
# Publish the selected pages under `docs/` to the repository's GitHub wiki.
#
# Create the first wiki page through GitHub to initialize its repository.
set -euo pipefail

dry_run=false
check_only=false
case "${1:-}" in
  --dry-run) dry_run=true; shift ;;
  --check) check_only=true; shift ;;
esac
if [ "$#" -ne 0 ]; then
  echo "usage: $0 [--dry-run|--check]" >&2
  exit 2
fi

source_dir=$(git rev-parse --show-toplevel)
cd "$source_dir"
manifest="docs/wiki-pages.txt"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
rendered="$tmp/rendered"
mkdir "$rendered"

[ -f "$manifest" ] || { echo "no wiki manifest at $manifest" >&2; exit 2; }

while IFS= read -r page; do
  [ -n "$page" ] || continue
  [ -f "docs/$page" ] || { echo "wiki page does not exist: docs/$page" >&2; exit 2; }
  cp "docs/$page" "$rendered/"
done < "$manifest"
# Copy referenced assets into the flat wiki repository.
if ls assets/architectures/*.svg > /dev/null 2>&1; then
  cp assets/architectures/*.svg "$rendered"/
fi
if [ -f assets/social-preview.png ]; then
  cp assets/social-preview.png "$rendered"/
fi
sed -i 's|\.\./assets/architectures/||g; s|\.\./assets/||g' "$rendered"/*.md
# GitHub wiki page links omit the `.md` suffix.
sed -i -E 's/\]\(([A-Za-z0-9_-]+)\.md(#[^)]*)?\)/](\1\2)/g' "$rendered"/*.md
# GitHub shows the page name as a title; omit the source page's first H1.
sed -i '0,/^# /{/^# /d;}' "$rendered"/*.md
python3 tools/check_links.py --wiki-root "$rendered"
if "$check_only"; then
  echo "generated wiki links passed"
  exit 0
fi

if [ -n "${WIKI_URL:-}" ]; then
  wiki_url="$WIKI_URL"
else
  repo_url=$(git remote get-url origin)
  wiki_url="${repo_url%.git}.wiki.git"
fi
wiki_dir="$tmp/wiki"
if ! git clone --quiet --depth 1 "$wiki_url" "$wiki_dir"; then
  echo "could not clone $wiki_url" >&2
  echo "create the wiki's first page through the web UI, then rerun" >&2
  exit 1
fi

# Remove pages that are no longer part of the public manual.
for page in "$wiki_dir"/*.md; do
  grep -Fxq "$(basename "$page")" "$manifest" || git -C "$wiki_dir" rm -q "$(basename "$page")"
done
cp "$rendered"/* "$wiki_dir/"
cd "$wiki_dir"
git add -A
if git diff --cached --quiet; then
  echo "wiki already matches docs/"
  exit 0
fi
if "$dry_run"; then
  git diff --cached --stat
  echo "dry run: wiki was not changed"
  exit 0
fi
git commit --quiet -m "Publish docs at $(git -C "$source_dir" rev-parse --short HEAD)"
git push --quiet
echo "published $(grep -cve '^$' "$source_dir/$manifest") pages to the wiki"
