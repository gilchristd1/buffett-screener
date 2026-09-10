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

# The screen run commits its own results (out/*.csv, summary.md) straight back
# to the repository, so GitHub is ahead of this clone after every run and a
# plain push is rejected as non-fast-forward. Rebasing local work on top of the
# runner's commits is always the right move here: the runner only ever touches
# out/, and Claude only ever touches code, so the two never collide.
echo "Catching up with results the runner has committed..."
if ! git pull --rebase --autostash --quiet; then
  echo >&2
  echo "The rebase did not complete cleanly. Nothing has been pushed." >&2
  echo "Run 'git status' and send Claude the output — do not force anything." >&2
  exit 1
fi

git add -A

# Commit only if there is something staged — but DO NOT exit here if there
# isn't. A commit that was made and then failed to push leaves nothing staged
# and everything still to send, and the first version of this script treated
# that as "nothing to do" and stopped. It cost a round trip: eleven files sat
# committed and unpushed while the script reported success.
if git diff --cached --quiet; then
  echo "No new file changes to commit."
else
  echo "About to commit these changes:"
  git diff --cached --stat
  echo
  git commit -q -m "${1:-Screener update}"
fi

# Now the real question: is anything waiting to go to GitHub?
UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo "")
if [ -n "$UPSTREAM" ]; then
  AHEAD=$(git rev-list --count "$UPSTREAM"..HEAD 2>/dev/null || echo 0)
else
  AHEAD=1     # no upstream yet, so the first push sets one
fi

if [ "$AHEAD" -eq 0 ]; then
  echo "Nothing to push — GitHub already has every local commit."
  exit 0
fi

echo "Pushing $AHEAD commit(s):"
git log --oneline "${UPSTREAM:-HEAD~$AHEAD}"..HEAD 2>/dev/null || git log --oneline -"$AHEAD"
echo
git push

echo
echo "Pushed. The screen run starts on its own for any change to a .py file,"
echo "overlay.csv, moats.csv, requirements.txt or the workflow — so there is"
echo "almost never anything to press in GitHub."
