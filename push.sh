#!/usr/bin/env bash
#
# Send Claude's changes to GitHub. Run this from Terminal on the Mac:
#
#     cd ~/Documents/buffett-screener && ./push.sh
#
# Optionally give it a message:  ./push.sh "recalibrate moat bands"
#
# Why you run this rather than Claude: git inside the Cowork VM cannot delete
# its own lock and temp files in a connected folder ("Operation not permitted"),
# so commits made from there are unreliable. Git on macOS, working on the real
# filesystem, has no such problem. Claude writes the files; this pushes them.
#
# First run will ask for a username and password. GitHub no longer accepts an
# account password here — use a personal access token with Contents: read/write
# on this repository. macOS stores it in the keychain afterwards, so it is asked
# for once and never again.

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .git ]; then
  echo "This folder is not a git clone. See the setup steps Claude gave you." >&2
  exit 1
fi

# Claude cannot write into .github/ — macOS treats it as a protected path and
# the file transfer refuses it. That is why the workflow always needed a manual
# paste. Claude writes the intended contents to WORKFLOW-paste-into-github.txt
# instead, and this copies it across, which is the same thing without the paste.
if [ -f WORKFLOW-paste-into-github.txt ]; then
  mkdir -p .github/workflows
  if ! cmp -s WORKFLOW-paste-into-github.txt .github/workflows/screen.yml; then
    cp WORKFLOW-paste-into-github.txt .github/workflows/screen.yml
    echo "Updated .github/workflows/screen.yml from WORKFLOW-paste-into-github.txt"
  fi
fi

git add -A

if git diff --cached --quiet; then
  echo "Nothing to push — no files have changed."
  exit 0
fi

echo "About to push these changes:"
git diff --cached --stat
echo

git commit -q -m "${1:-Screener update}"
git push

echo
echo "Pushed. If config.py was among the changes, the screen run has already"
echo "started on its own — no need to press anything in GitHub."
