#!/usr/bin/env bash
# One-time setup: create the GitHub repo, set the SEC contact secret, grant the
# workflow write access, and push. Requires the GitHub CLI:
#     brew install gh && gh auth login
set -euo pipefail

REPO_NAME="${1:-buffett-screener}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

# --- 1. SEC contact details --------------------------------------------------
# The SEC rejects requests without a name and contact email in the User-Agent.
# It is kept out of the repo (which is public) and stored as an Actions secret.
step "SEC contact details"
if [[ -z "${SEC_USER_AGENT:-}" ]]; then
  echo "The SEC requires a name and contact email on every request."
  read -r -p 'Enter it as "Your Name your@email.com": ' SEC_USER_AGENT
fi
if [[ "$SEC_USER_AGENT" != *"@"* ]]; then
  echo "That doesn't contain an email address. Aborting." >&2
  exit 1
fi
echo "  will be stored as the repository secret SEC_USER_AGENT, not committed"

# --- 2. Prove the logic works before publishing anything ---------------------
step "Running the test suite"
python3 tests/test_gates.py > /dev/null
python3 tests/test_screener_import.py > /dev/null
echo "  all checks pass"

# --- 3. Commit locally -------------------------------------------------------
step "Preparing the local repository"
git init -q 2>/dev/null || true
git add -A

EMAIL="$(printf '%s' "$SEC_USER_AGENT" | awk '{print $NF}')"
if git grep -qIn -- "$EMAIL" $(git diff --cached --name-only) 2>/dev/null; then
  echo "Your email appears in a staged file. Remove it before publishing." >&2
  exit 1
fi
echo "  no personal email in any staged file"

git commit -qm "Buffett screener: criteria v0.4 gate engine" || echo "  nothing new to commit"
git branch -M main

# --- 4. Create the repo, WITHOUT pushing yet ---------------------------------
# Order matters: the first push triggers the workflow (config.py is in it), so
# the secret and write permission must exist before any code arrives.
step "Creating the public repository"
gh repo create "$REPO_NAME" --public --source=. --remote=origin
OWNER="$(gh api user -q .login)"
echo "  created $OWNER/$REPO_NAME"

# --- 5. Secret and permissions, before the first push ------------------------
step "Configuring secrets and permissions"
gh secret set SEC_USER_AGENT --body "$SEC_USER_AGENT" --repo "$OWNER/$REPO_NAME"
echo "  secret SEC_USER_AGENT set"

# New repos often default to read-only workflow tokens, which would silently
# stop the run from committing results back.
gh api -X PUT "repos/$OWNER/$REPO_NAME/actions/permissions/workflow" \
  -f default_workflow_permissions=write \
  -F can_approve_pull_request_reviews=false > /dev/null
echo "  workflow granted write access (needed to commit results back)"

# --- 6. Push ------------------------------------------------------------------
step "Pushing"
git push -u origin main

cat <<EOF

Setup complete.

  Run the screen now:   gh workflow run 'Run screen'
  Watch it:             gh run watch
  See the result:       gh run view --log | tail -40

Results are committed back to the repo as out/survivors.csv, out/results.csv
and out/summary.md, and rerun automatically each quarter and on any config.py change.

Give Claude this URL to read each run:
  https://raw.githubusercontent.com/$OWNER/$REPO_NAME/main/out/summary.md

To run the screen locally as well:
  export SEC_USER_AGENT="$SEC_USER_AGENT"

The repo is public. It holds the methodology and public-company data only —
never your holdings, buy prices or position sizes. Those stay in the Claude
project. Keep it that way.
EOF
