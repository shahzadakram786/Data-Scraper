"""Checking for, downloading and applying updates.

An update is a version tag on GitHub — v2.1.0 — ideally with a GitHub Release
carrying notes on what changed. A plain commit is never an update: whoever
maintains the code decides when something is ready, by tagging it.

Two kinds of installation update themselves, detected automatically:

  * A git checkout (running `python app.py` from a clone). The tag is fetched
    and fast-forwarded to, new libraries are installed if requirements.txt
    changed, and the new version is test-imported in a separate process
    before it is kept. If that test fails the checkout is reset to exactly
    where it was.

  * The Windows .exe. The release's ProviderLeadGen.exe is downloaded, checked
    against its published SHA-256, and run once with --selfcheck. Only then is
    a small script left behind to swap it in after the running copy exits.

What is deliberately NOT done: replacing code inside the running process.
Reloading modules under live worker threads and a live Tk window is how a
scrape ends up half on old code and half on new, which produces bugs nobody
can reproduce. Updating means a restart of a few seconds. Leads, settings and
the saved mail password live outside the code, so they survive it untouched.

Nothing here updates on its own. The app checks and offers; a person clicks.
"""

import os
import re
import sys
import hashlib
import logging
import subprocess
import tempfile

import requests

from version import __version__


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPO = "shahzadakram786/Data-Scraper"
API_BASE = "https://api.github.com/repos/{repo}"

# Often enough that a fix reaches everyone the same working day; rare enough to
# stay far inside GitHub's 60 unauthenticated requests an hour.
CHECK_INTERVAL_SECONDS = 6 * 60 * 60

EXE_ASSET = "ProviderLeadGen.exe"
CHECKSUM_ASSET = EXE_ASSET + ".sha256"

# Only needed if the repository is made private: a read-only GitHub token in
# this environment variable lets the check see its releases.
TOKEN_ENV = "LEADGEN_GITHUB_TOKEN"

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class UpdateError(Exception):
    """An update could not be checked or applied.

    The message is shown to the user word for word, so it says what happened
    and what to do about it — never just that something failed.
    """


class UpdateInfo:
    """A published version newer than the one running."""

    def __init__(self, version, tag, notes="", url="", published_at="",
                 assets=None):
        self.version = version
        self.tag = tag
        self.notes = notes
        self.url = url
        self.published_at = published_at
        self.assets = assets or {}

    def __repr__(self):
        return "<UpdateInfo {0} ({1})>".format(self.version, self.tag)


# ------------------------------------------------------------------ versions

def parse_version(text):
    """"v2.1.0" or "2.1.0" -> (2, 1, 0). Anything else -> None."""
    match = _VERSION_RE.match(str(text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(candidate, current=__version__):
    """Compared as numbers: 2.10.0 is newer than 2.9.0."""
    new, old = parse_version(candidate), parse_version(current)
    return bool(new and old and new > old)


def install_kind(project_dir=PROJECT_DIR):
    """How this copy is installed: "frozen" (the .exe), "git" (a clone), or
    "unmanaged" (a folder of files with no way to fetch a newer one)."""
    if getattr(sys, "frozen", False):
        return "frozen"
    if os.path.isdir(os.path.join(project_dir, ".git")):
        return "git"
    return "unmanaged"


# --------------------------------------------------------------------- check

def _headers():
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "ProviderLeadGen/" + __version__}
    token = os.environ.get(TOKEN_ENV)
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def check_for_update(repo=DEFAULT_REPO, current=__version__, session=None,
                     timeout=15):
    """The newest published version if it is newer than `current`, else None.

    Prefers the latest GitHub Release, which carries notes. A repository that
    only has tags still works: the highest version tag is used. Raises
    UpdateError when GitHub could not be asked; the app keeps that quiet for
    background checks and shows it only when someone pressed Check now.
    """
    http = session or requests
    base = API_BASE.format(repo=repo)
    try:
        response = http.get(base + "/releases/latest", headers=_headers(),
                            timeout=timeout)
    except requests.RequestException as e:
        raise UpdateError(
            "Could not reach GitHub to check for updates ({0}). The app will "
            "try again later.".format(e))

    if response.status_code == 200:
        release = response.json()
        tag = release.get("tag_name") or ""
        parsed = parse_version(tag)
        if parsed is None:
            return None  # a release not named like a version is not an update
        info = UpdateInfo(
            version=".".join(str(part) for part in parsed),
            tag=tag,
            notes=(release.get("body") or "").strip(),
            url=release.get("html_url") or "",
            published_at=(release.get("published_at") or "")[:10],
            assets={asset.get("name"): asset.get("browser_download_url")
                    for asset in release.get("assets") or []
                    if asset.get("name")})
    elif response.status_code == 404:
        info = _latest_tag(http, repo, base, timeout)
        if info is None:
            return None
    elif response.status_code in (403, 429):
        raise UpdateError(
            "GitHub is limiting update checks from this network right now. The "
            "app will try again later.")
    else:
        raise UpdateError("GitHub answered HTTP {0} when checking for "
                          "updates.".format(response.status_code))

    return info if is_newer(info.version, current) else None


def _latest_tag(http, repo, base, timeout):
    """The highest version tag, for repositories that tag without releasing."""
    try:
        response = http.get(base + "/tags?per_page=100", headers=_headers(),
                            timeout=timeout)
    except requests.RequestException as e:
        raise UpdateError(
            "Could not reach GitHub to check for updates ({0}).".format(e))
    if response.status_code == 404:
        raise UpdateError(
            "The update repository {0} was not found. If it has been made "
            "private, the app needs a read-only GitHub token in the {1} "
            "environment variable to see it.".format(repo, TOKEN_ENV))
    if response.status_code != 200:
        raise UpdateError("GitHub answered HTTP {0} when listing versions."
                          .format(response.status_code))

    versions = [(parse_version(tag.get("name")), tag.get("name"))
                for tag in response.json() or []
                if isinstance(tag, dict) and parse_version(tag.get("name"))]
    if not versions:
        return None
    best, tag = max(versions)
    return UpdateInfo(version=".".join(str(part) for part in best), tag=tag,
                      url="https://github.com/{0}/tree/{1}".format(repo, tag))


# ------------------------------------------------------- apply: git checkout

def apply_git_update(info, repo_dir=PROJECT_DIR, logger=None,
                     run=subprocess.run, python=sys.executable):
    """Move a git checkout forward to info.tag, or leave it exactly as it was.

    Returns the commit it moved from. Refuses — rather than risking anyone's
    work — when the app's own files have local changes, or when this copy has
    commits the release does not contain.
    """
    log = logger or logging.getLogger(__name__)

    def git(*args, check=True):
        result = run(["git"] + list(args), cwd=repo_dir, capture_output=True,
                     text=True)
        if check and result.returncode != 0:
            raise UpdateError("git {0} failed: {1}".format(
                args[0], _last_line(result.stderr or result.stdout)))
        return result

    if git("rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        raise UpdateError(
            "This copy of the app is not a git checkout, so it cannot update "
            "itself. Download version {0} from GitHub instead.".format(info.version))

    changed = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if changed:
        files = [line[3:].strip() for line in changed.splitlines()]
        raise UpdateError(
            "Some of the app's own files have been changed on this computer "
            "({0}). Updating would overwrite those changes, so nothing was "
            "touched. Commit or undo them, then try again.".format(
                ", ".join(files[:5]) + (" and more" if len(files) > 5 else "")))

    before = git("rev-parse", "HEAD").stdout.strip()

    fetched = git("fetch", "--tags", "origin", check=False)
    if fetched.returncode != 0:
        raise UpdateError("Could not download the update from GitHub: " +
                          _last_line(fetched.stderr or fetched.stdout))
    if git("rev-parse", "--verify", "--quiet", info.tag + "^{commit}",
           check=False).returncode != 0:
        raise UpdateError(
            "Version {0} was announced, but its tag is not on GitHub yet. Try "
            "again in a few minutes.".format(info.version))

    old_requirements = git("show", "HEAD:requirements.txt", check=False).stdout
    new_requirements = git("show", info.tag + ":requirements.txt",
                           check=False).stdout

    merged = git("merge", "--ff-only", info.tag, check=False)
    if merged.returncode != 0:
        raise UpdateError(
            "This copy has its own commits that are not part of version {0}, "
            "so it cannot move to it automatically without losing them. "
            "Whoever maintains the code needs to merge the release by hand."
            .format(info.version))

    try:
        if old_requirements != new_requirements:
            log.info("The update changes the libraries the app needs — "
                     "installing them…")
            installed = run([python, "-m", "pip", "install", "-r",
                             "requirements.txt"], cwd=repo_dir,
                            capture_output=True, text=True)
            if installed.returncode != 0:
                raise UpdateError(
                    "The libraries version {0} needs could not be installed: {1}"
                    .format(info.version,
                            _last_line(installed.stderr or installed.stdout)))

        # Import the new code in a separate process before trusting it. A
        # syntax error or a missing library then costs a clear message here,
        # not an app that no longer opens.
        started = run([python, "-c", "import app"], cwd=repo_dir,
                      capture_output=True, text=True, timeout=180)
        if started.returncode != 0:
            raise UpdateError(
                "Version {0} failed to start on this computer, so the version "
                "you had has been kept. The error was: {1}".format(
                    info.version, _last_line(started.stderr or started.stdout)))
    except subprocess.TimeoutExpired:
        git("reset", "--hard", before, check=False)
        raise UpdateError("Version {0} took too long to start, so the version "
                          "you had has been kept.".format(info.version))
    except UpdateError:
        # Safe to reset: the tree had no local changes when this began, so the
        # only difference from `before` is the update itself.
        git("reset", "--hard", before, check=False)
        log.warning("Update to %s rolled back to %s.", info.version, before[:8])
        raise

    log.info("Updated from %s to %s.", before[:8], info.tag)
    return before


# -------------------------------------------------------- apply: Windows .exe

def apply_frozen_update(info, exe_path=None, logger=None, session=None,
                        run=subprocess.run):
    """Download, verify and self-test the new .exe next to the running one.

    Returns the path of the verified download. The running .exe cannot replace
    itself on Windows while it is open, so swapping is left to the script from
    write_swap_script, which waits for this process to exit.
    """
    log = logger or logging.getLogger(__name__)
    exe_path = exe_path or sys.executable
    url = info.assets.get(EXE_ASSET)
    checksum_url = info.assets.get(CHECKSUM_ASSET)
    if not url or not checksum_url:
        raise UpdateError(
            "Version {0} was published without a Windows build and its checksum "
            "attached, so this installation cannot update itself. Download it "
            "from GitHub instead.".format(info.version))

    http = session or requests
    try:
        answer = http.get(checksum_url, headers=_headers(), timeout=30)
        answer.raise_for_status()
        expected = (answer.text.split() or [""])[0].strip().lower()
    except requests.RequestException as e:
        raise UpdateError("Could not download the update's checksum: {0}".format(e))
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise UpdateError("The update's published checksum is not a valid "
                          "SHA-256, so the download was not attempted.")

    new_path = exe_path + ".new"
    digest = hashlib.sha256()
    try:
        with http.get(url, headers=_headers(), stream=True,
                      timeout=(10, 120)) as response:
            response.raise_for_status()
            with open(new_path, "wb") as handle:
                for chunk in response.iter_content(1 << 16):
                    if chunk:
                        handle.write(chunk)
                        digest.update(chunk)
    except (requests.RequestException, OSError) as e:
        _remove(new_path)
        raise UpdateError("The download of version {0} failed: {1}".format(
            info.version, e))

    if digest.hexdigest() != expected:
        _remove(new_path)
        raise UpdateError(
            "The downloaded file did not match its published checksum, so it "
            "was discarded. Nothing has changed. Try again later.")

    try:
        started = run([new_path, "--selfcheck"], capture_output=True, text=True,
                      timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        _remove(new_path)
        raise UpdateError("Version {0} could not be started for its check: {1}"
                          .format(info.version, e))
    if started.returncode != 0:
        _remove(new_path)
        raise UpdateError("Version {0} failed its start-up check, so the version "
                          "you had has been kept.".format(info.version))

    log.info("Downloaded and verified version %s.", info.version)
    return new_path


def write_swap_script(exe_path, new_path, pid, updated_from, directory=None):
    """A batch script that waits for the running app to exit, swaps the .exe
    (keeping the previous one as .old, and restoring it if the swap fails),
    starts the new version, and deletes itself."""
    directory = directory or tempfile.gettempdir()
    script = os.path.join(directory, "providerleadgen-update-{0}.bat".format(pid))
    lines = [
        "@echo off",
        ":wait",
        'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul',
        "if not errorlevel 1 (",
        "  timeout /t 1 /nobreak >nul",
        "  goto wait",
        ")",
        'move /Y "{exe}" "{exe}.old" >nul',
        'move /Y "{new}" "{exe}" >nul',
        'if errorlevel 1 move /Y "{exe}.old" "{exe}" >nul',
        'start "" "{exe}" --updated-from {old}',
        '(goto) 2>nul & del "%~f0"',
    ]
    body = "\r\n".join(lines).format(pid=int(pid), exe=exe_path, new=new_path,
                                     old=updated_from)
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(body + "\r\n")
    return script


# ------------------------------------------------------------------- restart

def restart(updated_from, swap_script=None, app_path=None, cwd=PROJECT_DIR,
            popen=subprocess.Popen):
    """Start the updated app as a separate process; the caller then closes.

    The new process is detached, so it outlives the one that launched it.
    """
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    if swap_script:
        command = ["cmd", "/c", swap_script]
    else:
        command = [_windowless_python(),
                   app_path or os.path.join(PROJECT_DIR, "app.py"),
                   "--updated-from", str(updated_from)]
    return popen(command, cwd=cwd, creationflags=flags, close_fds=True)


def _windowless_python():
    """pythonw.exe when it sits next to the running interpreter, so the
    restarted app does not come back with a stray console window."""
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.exists(candidate) else sys.executable


# ------------------------------------------------------------------- helpers

def _last_line(text):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return (lines[-1] if lines else "no details were given")[:300]


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
