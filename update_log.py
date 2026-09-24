"""Tracks which commits the running bot is actually deployed at, so a
private channel can get a short changelog each time it comes back up on
new code.

Deliberately not wired to /setchannel or any command -- the destination is
a single hardcoded channel in bot.py, not a per-guild opt-in feature, so
it can't be turned on anywhere else.

Reads the deployed checkout's own git history rather than a hand-written
CHANGELOG, so nothing has to be remembered at release time. If the deploy
has no .git directory (files copied rather than pulled) or git isn't on
PATH, every function here returns "nothing to report" and the feature is
silently inert rather than erroring on startup.
"""
import os
import json
import logging
import subprocess

logger = logging.getLogger(__name__)

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, 'update_log_state.json')

# Past this many commits in one deploy the list stops being a "quick
# summary" and starts being a wall -- the rest get collapsed into a count.
MAX_LISTED_COMMITS = 15

# ASCII unit separator: can't occur inside a commit subject, unlike any
# punctuation that might.
_FIELD_SEP = '\x1f'


def _run_git(*args: str) -> str | None:
    """Returns stdout, or None if git isn't available, this isn't a
    checkout, or the command failed -- callers treat all three the same."""
    try:
        result = subprocess.run(
            ['git', *args], cwd=BASE, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug(f"update_log: git {' '.join(args)} unavailable: {e}")
        return None
    if result.returncode != 0:
        logger.debug(f"update_log: git {' '.join(args)} failed: {result.stderr.strip()}")
        return None
    return result.stdout.strip()


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except (ValueError, OSError) as e:
            # A truncated write (power loss mid-deploy) shouldn't wedge
            # startup -- treat it as a first run and re-seed.
            logger.warning(f"update_log: unreadable state file, re-seeding: {e}")
    return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except OSError as e:
        logger.warning(f"update_log: couldn't write state file: {e}")


def _commit_exists(sha: str) -> bool:
    """Whether `sha` is still reachable in this checkout. It won't be
    after a force-push, a rebase, or if the deploy is a shallow clone that
    was re-cloned -- in which case `git log sha..HEAD` would error out."""
    return _run_git('cat-file', '-e', f'{sha}^{{commit}}') is not None


def _commits_since(sha: str | None) -> list[tuple[str, str]]:
    """(short_sha, subject) for each non-merge commit after `sha`, oldest
    first so the channel reads in the order the work happened."""
    fmt = f'--pretty=format:%h{_FIELD_SEP}%s'
    revs = f'{sha}..HEAD' if sha else '-1'
    output = _run_git('log', '--no-merges', '--reverse', fmt, revs)
    if not output:
        return []

    commits = []
    for line in output.splitlines():
        short, _, subject = line.partition(_FIELD_SEP)
        if subject:
            commits.append((short, subject))
    return commits


def _format_update(commits: list[tuple[str, str]], head: str) -> str:
    listed = commits[:MAX_LISTED_COMMITS]
    hidden = len(commits) - len(listed)

    count = f"**{len(commits)} new commit{'s' if len(commits) != 1 else ''}**"
    lines = [f"## 🔧 Bot Updated", f"{count} — now running `{head}`", ""]
    lines += [f"• {subject}" for _, subject in listed]
    if hidden:
        lines.append(f"• …and {hidden} more")
    return "\n".join(lines)


def pending_update() -> str | None:
    """Returns a short changelog of everything deployed since the last
    time this reported, or None if there's nothing new.

    Stays quiet on the very first run, recording whatever is deployed as
    the baseline -- otherwise the first startup after adding this would
    dump the entire project history into the channel.

    Records the new HEAD before the message is sent rather than after, so
    a send that fails (channel gone, missing permissions, bot crashing on
    boot) can't leave the same changelog queued to be re-posted on every
    subsequent restart.
    """
    head = _run_git('rev-parse', '--short', 'HEAD')
    if not head:
        return None  # not a git checkout, or git unavailable -- stay inert

    state = _load_state()
    last = state.get('last_commit')

    if last == head:
        return None  # restarted on the same code; a restart isn't an update

    if last and not _commit_exists(last):
        # History was rewritten or re-cloned underneath us. Listing
        # "everything since a commit that no longer exists" isn't
        # meaningful, so re-baseline silently instead of dumping history.
        logger.info(f"update_log: recorded commit {last} is gone; re-seeding at {head}")
        last = None

    commits = _commits_since(last) if last else []

    state['last_commit'] = head
    _save_state(state)

    if not commits:
        return None  # first run, or a re-seed: baseline recorded, stay quiet

    return _format_update(commits, head)
