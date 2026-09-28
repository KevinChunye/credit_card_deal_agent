#!/bin/sh
# One-shot setup/repair for the card agent on a Maritime OpenClaw container.
# Idempotent: safe to re-run after restarts, git updates, or fresh agents.
#
# Usage (in the agent's Console tab):
#   sh /data/.openclaw/workspace/skills/credit_card_deal_agent/deploy/maritime_setup.sh
# Fresh container without the repo yet:
#   git clone https://github.com/KevinChunye/credit_card_deal_agent \
#     /data/.openclaw/workspace/skills/credit_card_deal_agent
#   sh /data/.openclaw/workspace/skills/credit_card_deal_agent/deploy/maritime_setup.sh
# (Private repo: clone with a token URL, or set GITHUB_TOKEN for `sync`.)
set -e

SKILL_DIR="${CARD_AGENT_HOME:-/data/.openclaw/workspace/skills/credit_card_deal_agent}"
REPO_URL="https://github.com/KevinChunye/credit_card_deal_agent"
echo "== credit_card_deal_agent setup =="

# Write protection: the agent operates this tool, it never edits it. Code and
# skill files are made immutable (chattr +i; chmod a-w as a fallback). .git/
# and __pycache__/ stay writable. This script is the only path that unlocks,
# updates via git, and re-locks. Private state lives OUTSIDE the repo
# (CARD_AGENT_DB), so locking never touches it.
lock_tree() {
    [ -d "$1" ] || return 0
    find "$1" \( -name .git -o -name __pycache__ \) -prune -o -type f -print | while read -r f; do
        chattr +i "$f" 2>/dev/null || chmod a-w "$f" 2>/dev/null || true
    done
    # chmod doesn't stop root; only the immutable flag does. Say so if it's missing.
    if command -v lsattr >/dev/null 2>&1 && ! lsattr "$1/SKILL.md" 2>/dev/null | cut -d' ' -f1 | grep -q i; then
        echo "WARNING: couldn't set the immutable flag (chattr); the agent could still edit its files."
    fi
}
unlock_tree() {
    [ -d "$1" ] || return 0
    find "$1" -name .git -prune -o -type f -exec chattr -i {} \; 2>/dev/null
    chmod -R u+w "$1" 2>/dev/null || true
}

# 0. Unlock so git can update, and re-lock on every exit, even a failed one.
unlock_tree "$SKILL_DIR"
trap 'lock_tree "$SKILL_DIR"' EXIT

# 1. Code: clone or update.
if [ -d "$SKILL_DIR/.git" ]; then
    if ! git -C "$SKILL_DIR" pull --ff-only; then
        echo "ERROR: git pull failed, so this is still the old version. See what's different with:"
        echo "  git -C $SKILL_DIR status"
        echo "The agent never edits these files, so it's safe to reset to GitHub's version:"
        echo "  git -C $SKILL_DIR fetch origin && git -C $SKILL_DIR reset --hard origin/main"
        exit 1
    fi
else
    mkdir -p "$(dirname "$SKILL_DIR")"
    git clone "$REPO_URL" "$SKILL_DIR"
fi

# 2. pip (Debian images ship python3 without it).
if ! python3 -m pip --version >/dev/null 2>&1; then
    python3 -m ensurepip --upgrade 2>/dev/null \
        || { apt-get update && apt-get install -y python3-pip; }
fi

# 3. Dependencies (PEP 668 flag needed on Debian 12; fall back without it).
python3 -m pip install --break-system-packages -q -r "$SKILL_DIR/requirements.txt" "pytest>=8,<10" 2>/dev/null \
    || python3 -m pip install -q -r "$SKILL_DIR/requirements.txt" "pytest>=8,<10"

# 4. Verify the deterministic core (offline tests).
cd "$SKILL_DIR"
python3 -m pytest -q

# 5. Private state location: warn if it would land somewhere non-persistent.
if [ -z "$CARD_AGENT_DB" ]; then
    case "$HOME" in
        /data*) ;;
        *) echo "WARNING: CARD_AGENT_DB is not set and HOME ($HOME) is not under /data."
           echo "         Set CARD_AGENT_DB=/data/.credit_card_deal_agent/state.db in Maritime's"
           echo "         environment settings so your profile survives restarts." ;;
    esac
fi

# 6. First snapshot (fine if the collector hasn't run yet).
chmod +x "$SKILL_DIR/bin/card-agent" 2>/dev/null || true
python3 -m card_agent sync || echo "(sync failed: has the Collector workflow run and created the data branch?)"

# 7. Re-lock code and skill files so the agent cannot modify them (the EXIT trap
# does it too, which also covers a failed step above).
lock_tree "$SKILL_DIR" && echo "locked (read-only) -> $SKILL_DIR"

echo "== DONE. Restart the agent (Sleep, then send a chat message), then ask it: 'list your skills' =="
