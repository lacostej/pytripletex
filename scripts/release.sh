#!/usr/bin/env bash
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "usage: $0 <patch|minor|major|X.Y.Z>" >&2
    exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "error: working tree has uncommitted changes — commit or stash first" >&2
    git status --short >&2
    exit 1
fi

uvx hatch version "$1"
NEW=$(uvx hatch version)

git add src/tripletex/__about__.py
git commit -m "Bump version to $NEW"
git tag -a "v$NEW" -m "v$NEW"

echo
echo "Bumped to $NEW. Run 'git push && git push --tags' to publish."
