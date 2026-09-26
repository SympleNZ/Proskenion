#!/usr/bin/env bash
# publish-public.sh — snapshot main into the `public` branch for the public
# GitHub repository, leaving out what is kept private (§20.3).
#
# The development history stays local: the public branch is its own line of
# history, one commit per publication, each holding main's tree minus the
# excluded paths. Nothing in the working tree or in main's history changes.
#
#   tools/publish-public.sh "Release v0.1.6"      # build the commit
#   git push origin public:main                   # then publish it
#
# Kept private, and why:
#   WORKLOG.md, CLAUDE.md, docs/CLAUDE-CODE-BRIEF.md — how the build was
#       coordinated, not how the system works
#   docs/plans/* except *-contracts.md — planning notes; the contracts are the
#       API shapes the code cites, so they are published
#   docs/phase-*-milestone.md — milestone audits
#   docs/protocols/*.pdf — vendor manuals, not ours to redistribute
# HANDOVER.md is never tracked at all (.gitignore).

set -euo pipefail

message="${1:?usage: tools/publish-public.sh \"commit message\"}"
source_ref="${SOURCE_REF:-main}"
branch="public"

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

index="$(mktemp)"
trap 'rm -f "$index"' EXIT
export GIT_INDEX_FILE="$index"

git read-tree "$source_ref"

exclude=(
    WORKLOG.md
    CLAUDE.md
    docs/CLAUDE-CODE-BRIEF.md
)
while IFS= read -r path; do
    case "$path" in
        docs/plans/*-contracts.md) ;;
        docs/plans/*) exclude+=("$path") ;;
        docs/phase-*-milestone.md) exclude+=("$path") ;;
        docs/protocols/*.pdf) exclude+=("$path") ;;
    esac
done < <(git ls-files)

git rm --cached --quiet --ignore-unmatch -- "${exclude[@]}"

# Never publish a tree that still holds something private.
if git ls-files | grep -qxE 'HANDOVER\.md|WORKLOG\.md|CLAUDE\.md|.*\.(key|pem|p12|pfx)|docs/protocols/.*\.pdf'; then
    echo "publish-public: a private path is still in the tree; refusing" >&2
    git ls-files | grep -xE 'HANDOVER\.md|WORKLOG\.md|CLAUDE\.md|.*\.(key|pem|p12|pfx)|docs/protocols/.*\.pdf' >&2
    exit 1
fi

tree="$(git write-tree)"
unset GIT_INDEX_FILE

parent_args=()
if git rev-parse --verify --quiet "refs/heads/$branch" >/dev/null; then
    if [ "$(git rev-parse "$branch^{tree}")" = "$tree" ]; then
        echo "publish-public: nothing new since the last publication"
        exit 0
    fi
    parent_args=(-p "$(git rev-parse "$branch")")
fi

# The public commit carries its own identity, so a private work address never
# reaches the public history. Set once per clone:
#   git config proskenion.publicName  "Your Name"
#   git config proskenion.publicEmail "12345+you@users.noreply.github.com"
public_name="$(git config --get proskenion.publicName || true)"
public_email="$(git config --get proskenion.publicEmail || true)"
if [ -z "$public_name" ] || [ -z "$public_email" ]; then
    echo "publish-public: set proskenion.publicName and proskenion.publicEmail first" >&2
    exit 1
fi
commit="$(
    GIT_AUTHOR_NAME="$public_name" GIT_AUTHOR_EMAIL="$public_email" \
    GIT_COMMITTER_NAME="$public_name" GIT_COMMITTER_EMAIL="$public_email" \
    git commit-tree "$tree" "${parent_args[@]}" -m "$message"
)"
git update-ref "refs/heads/$branch" "$commit"

echo "publish-public: $branch is now $(git rev-parse --short "$commit") ($(git ls-tree -r --name-only "$commit" | wc -l) files)"
echo "publish-public: excluded ${#exclude[@]} paths; review, then: git push origin $branch:main"
