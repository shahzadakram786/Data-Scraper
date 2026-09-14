"""Updates: deciding what counts as newer, and applying one without ever
leaving a copy half-updated.

The git tests use real repositories in a temporary folder — a release tag, a
clone, a fast-forward — because the failure modes that matter (local changes,
diverged history, a new version that does not start) live in git's behaviour,
not in anything a fake would reproduce. Nothing here touches the network.
"""

import hashlib
import os
import subprocess
import sys

import pytest

import updater
from updater import UpdateError, UpdateInfo


# ------------------------------------------------------------------ versions

class TestVersions:

    @pytest.mark.parametrize("text, expected", [
        ("v2.1.0", (2, 1, 0)),
        ("2.1.0", (2, 1, 0)),
        ("  v10.0.3 ", (10, 0, 3)),
        ("v2.1", None),
        ("latest", None),
        ("v2.1.0-beta", None),
        ("", None),
        (None, None),
    ])
    def test_parsing(self, text, expected):
        assert updater.parse_version(text) == expected

    @pytest.mark.parametrize("candidate, current, newer", [
        ("v2.1.0", "2.0.0", True),
        ("v2.0.1", "2.0.0", True),
        ("v3.0.0", "2.9.9", True),
        ("v2.10.0", "2.9.0", True),     # numbers, not text: 10 > 9
        ("v2.0.0", "2.0.0", False),
        ("v1.9.0", "2.0.0", False),
        ("nonsense", "2.0.0", False),
    ])
    def test_comparison(self, candidate, current, newer):
        assert updater.is_newer(candidate, current) is newer

    def test_the_running_version_is_well_formed(self):
        assert updater.parse_version(updater.__version__) is not None


class TestInstallKind:

    def test_the_exe_is_recognised(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert updater.install_kind(str(tmp_path)) == "frozen"

    def test_a_git_clone_is_recognised(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert updater.install_kind(str(tmp_path)) == "git"

    def test_a_plain_folder_cannot_update_itself(self, tmp_path):
        assert updater.install_kind(str(tmp_path)) == "unmanaged"


# --------------------------------------------------------------------- check

class FakeResponse:
    def __init__(self, status=200, payload=None, text="", chunks=None):
        self.status_code = status
        self._payload = payload
        self.text = text
        self._chunks = chunks or []

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError("HTTP {0}".format(self.status_code))

    def iter_content(self, size):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGitHub:
    """Answers the two GitHub API calls the check makes."""

    def __init__(self, release=None, release_status=200, tags=None,
                 tags_status=200, error=None):
        self.release = release
        self.release_status = release_status
        self.tags = tags or []
        self.tags_status = tags_status
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if self.error:
            raise self.error
        if url.endswith("/releases/latest"):
            return FakeResponse(self.release_status, self.release)
        if "/tags" in url:
            return FakeResponse(self.tags_status, self.tags)
        raise AssertionError("unexpected call " + url)


class TestCheck:

    def test_a_newer_release_is_offered_with_its_notes(self):
        github = FakeGitHub(release={
            "tag_name": "v2.1.0", "body": "Faster exports.\n",
            "html_url": "https://github.com/x/y/releases/tag/v2.1.0",
            "published_at": "2026-09-20T10:00:00Z",
            "assets": [{"name": "ProviderLeadGen.exe",
                        "browser_download_url": "https://dl/exe"}]})
        info = updater.check_for_update(current="2.0.0", session=github)
        assert info.version == "2.1.0"
        assert info.tag == "v2.1.0"
        assert info.notes == "Faster exports."
        assert info.published_at == "2026-09-20"
        assert info.assets["ProviderLeadGen.exe"] == "https://dl/exe"

    def test_the_same_version_is_not_an_update(self):
        github = FakeGitHub(release={"tag_name": "v2.0.0"})
        assert updater.check_for_update(current="2.0.0", session=github) is None

    def test_an_older_release_is_not_an_update(self):
        github = FakeGitHub(release={"tag_name": "v1.5.0"})
        assert updater.check_for_update(current="2.0.0", session=github) is None

    def test_a_release_not_named_like_a_version_is_ignored(self):
        github = FakeGitHub(release={"tag_name": "nightly"})
        assert updater.check_for_update(current="2.0.0", session=github) is None

    def test_without_releases_the_highest_version_tag_is_used(self):
        """Tagging and pushing is enough; a GitHub Release is optional."""
        github = FakeGitHub(release_status=404, tags=[
            {"name": "v2.0.1"}, {"name": "v2.10.0"}, {"name": "v2.9.0"},
            {"name": "experiment"}])
        info = updater.check_for_update(current="2.0.0", session=github)
        assert info.tag == "v2.10.0"
        assert info.notes == ""

    def test_no_releases_and_no_tags_means_no_update(self):
        github = FakeGitHub(release_status=404, tags=[])
        assert updater.check_for_update(current="2.0.0", session=github) is None

    def test_a_missing_repository_explains_the_private_repo_case(self):
        github = FakeGitHub(release_status=404, tags_status=404)
        with pytest.raises(UpdateError, match=updater.TOKEN_ENV):
            updater.check_for_update(current="2.0.0", session=github)

    def test_being_offline_is_an_update_error_not_a_crash(self):
        import requests
        github = FakeGitHub(error=requests.ConnectionError("no route"))
        with pytest.raises(UpdateError, match="Could not reach GitHub"):
            updater.check_for_update(current="2.0.0", session=github)

    def test_rate_limiting_is_explained(self):
        github = FakeGitHub(release_status=403)
        with pytest.raises(UpdateError, match="limiting"):
            updater.check_for_update(current="2.0.0", session=github)


# -------------------------------------------------------- apply: git checkout

def _git(cwd, *args):
    result = subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test",
         "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false"] + list(args),
        cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _write(path, text):
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def released(tmp_path):
    """An upstream repository at v2.0.0 and a clone still on v1.0.0."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git is not installed")

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _write(upstream / "version.py", '__version__ = "1.0.0"\n')
    _write(upstream / "app.py", "VALUE = 1\n")
    _write(upstream / "requirements.txt", "requests\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "one")
    _git(upstream, "tag", "v1.0.0")

    install = tmp_path / "install"
    _git(tmp_path, "clone", "-q", str(upstream), str(install))

    _write(upstream / "version.py", '__version__ = "2.0.0"\n')
    _write(upstream / "app.py", "VALUE = 2\n")
    _git(upstream, "commit", "-q", "-am", "two")
    _git(upstream, "tag", "v2.0.0")

    return {"upstream": upstream, "install": install}


def _version_in(folder):
    return (folder / "version.py").read_text(encoding="utf-8")


class TestGitUpdate:

    def test_the_checkout_moves_forward_to_the_release(self, released):
        before = _git(released["install"], "rev-parse", "HEAD")
        returned = updater.apply_git_update(
            UpdateInfo("2.0.0", "v2.0.0"), repo_dir=str(released["install"]))
        assert returned == before
        assert '"2.0.0"' in _version_in(released["install"])

    def test_changed_requirements_install_the_new_libraries(self, released):
        _write(released["upstream"] / "requirements.txt", "requests\nnewlibrary\n")
        _git(released["upstream"], "commit", "-q", "-am", "needs a library")
        _git(released["upstream"], "tag", "v2.1.0")

        pip_calls = []

        def run(command, **kwargs):
            if command[1:3] == ["-m", "pip"]:
                pip_calls.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.run(command, **kwargs)

        updater.apply_git_update(UpdateInfo("2.1.0", "v2.1.0"),
                                 repo_dir=str(released["install"]), run=run)
        assert len(pip_calls) == 1

    def test_unchanged_requirements_do_not_run_pip(self, released):
        pip_calls = []

        def run(command, **kwargs):
            if command[1:3] == ["-m", "pip"]:
                pip_calls.append(command)
            return subprocess.run(command, **kwargs)

        updater.apply_git_update(UpdateInfo("2.0.0", "v2.0.0"),
                                 repo_dir=str(released["install"]), run=run)
        assert pip_calls == []

    def test_a_version_that_does_not_start_is_rolled_back(self, released):
        """The one guarantee that matters: an update never leaves an app that
        no longer opens."""
        _write(released["upstream"] / "app.py", "raise RuntimeError('broken build')\n")
        _write(released["upstream"] / "version.py", '__version__ = "2.0.1"\n')
        _git(released["upstream"], "commit", "-q", "-am", "broken")
        _git(released["upstream"], "tag", "v2.0.1")
        before = _git(released["install"], "rev-parse", "HEAD")

        with pytest.raises(UpdateError, match="kept"):
            updater.apply_git_update(UpdateInfo("2.0.1", "v2.0.1"),
                                     repo_dir=str(released["install"]))

        assert _git(released["install"], "rev-parse", "HEAD") == before
        assert '"1.0.0"' in _version_in(released["install"])

    def test_local_changes_to_the_apps_files_stop_the_update(self, released):
        _write(released["install"] / "app.py", "VALUE = 'edited by hand'\n")
        before = _git(released["install"], "rev-parse", "HEAD")

        with pytest.raises(UpdateError, match="changed on this computer"):
            updater.apply_git_update(UpdateInfo("2.0.0", "v2.0.0"),
                                     repo_dir=str(released["install"]))

        assert _git(released["install"], "rev-parse", "HEAD") == before
        assert "edited by hand" in (released["install"] / "app.py").read_text()

    def test_untracked_files_such_as_leads_do_not_block_it(self, released):
        """The database, settings and exports are not part of the code."""
        _write(released["install"] / "leads.db", "not really a database")
        _write(released["install"] / "config.json", "{}")
        updater.apply_git_update(UpdateInfo("2.0.0", "v2.0.0"),
                                 repo_dir=str(released["install"]))
        assert (released["install"] / "leads.db").exists()
        assert (released["install"] / "config.json").exists()

    def test_a_copy_with_its_own_commits_is_not_forced(self, released):
        _write(released["install"] / "notes.txt", "local work\n")
        _git(released["install"], "add", "notes.txt")
        _git(released["install"], "commit", "-q", "-m", "local work")
        local = _git(released["install"], "rev-parse", "HEAD")

        with pytest.raises(UpdateError, match="its own commits"):
            updater.apply_git_update(UpdateInfo("2.0.0", "v2.0.0"),
                                     repo_dir=str(released["install"]))

        assert _git(released["install"], "rev-parse", "HEAD") == local

    def test_a_tag_that_is_not_published_yet_is_explained(self, released):
        with pytest.raises(UpdateError, match="not on GitHub yet"):
            updater.apply_git_update(UpdateInfo("9.9.9", "v9.9.9"),
                                     repo_dir=str(released["install"]))

    def test_a_folder_that_is_not_a_checkout_is_explained(self, tmp_path):
        with pytest.raises(UpdateError, match="not a git checkout"):
            updater.apply_git_update(UpdateInfo("2.0.0", "v2.0.0"),
                                     repo_dir=str(tmp_path))


# -------------------------------------------------------- apply: Windows .exe

class FakeDownloads:
    def __init__(self, payload, checksum):
        self.payload = payload
        self.checksum = checksum

    def get(self, url, **kwargs):
        if url.endswith(".sha256"):
            return FakeResponse(200, text=self.checksum + "  ProviderLeadGen.exe\n")
        return FakeResponse(200, chunks=[self.payload[:5], self.payload[5:]])


def _exe_release():
    return UpdateInfo("2.1.0", "v2.1.0", assets={
        updater.EXE_ASSET: "https://dl/ProviderLeadGen.exe",
        updater.CHECKSUM_ASSET: "https://dl/ProviderLeadGen.exe.sha256"})


class TestExeUpdate:

    PAYLOAD = b"MZ pretend windows executable"

    def _passes_selfcheck(self, command, **kwargs):
        assert command[1] == "--selfcheck"
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_a_verified_download_is_kept_beside_the_running_exe(self, tmp_path):
        exe = tmp_path / "ProviderLeadGen.exe"
        exe.write_bytes(b"old")
        good = hashlib.sha256(self.PAYLOAD).hexdigest()

        new_path = updater.apply_frozen_update(
            _exe_release(), exe_path=str(exe),
            session=FakeDownloads(self.PAYLOAD, good), run=self._passes_selfcheck)

        assert new_path == str(exe) + ".new"
        assert (tmp_path / "ProviderLeadGen.exe.new").read_bytes() == self.PAYLOAD
        assert exe.read_bytes() == b"old", "the running exe is not touched yet"

    def test_a_download_that_does_not_match_its_checksum_is_discarded(self, tmp_path):
        exe = tmp_path / "ProviderLeadGen.exe"
        exe.write_bytes(b"old")
        wrong = hashlib.sha256(b"something else").hexdigest()

        with pytest.raises(UpdateError, match="checksum"):
            updater.apply_frozen_update(
                _exe_release(), exe_path=str(exe),
                session=FakeDownloads(self.PAYLOAD, wrong), run=self._passes_selfcheck)

        assert not (tmp_path / "ProviderLeadGen.exe.new").exists()

    def test_a_build_that_fails_its_selfcheck_is_discarded(self, tmp_path):
        exe = tmp_path / "ProviderLeadGen.exe"
        good = hashlib.sha256(self.PAYLOAD).hexdigest()

        def fails(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "boom")

        with pytest.raises(UpdateError, match="start-up check"):
            updater.apply_frozen_update(
                _exe_release(), exe_path=str(exe),
                session=FakeDownloads(self.PAYLOAD, good), run=fails)

        assert not (tmp_path / "ProviderLeadGen.exe.new").exists()

    def test_a_release_without_a_windows_build_is_explained(self, tmp_path):
        with pytest.raises(UpdateError, match="without a Windows build"):
            updater.apply_frozen_update(UpdateInfo("2.1.0", "v2.1.0"),
                                        exe_path=str(tmp_path / "x.exe"))

    def test_an_invalid_published_checksum_stops_before_downloading(self, tmp_path):
        with pytest.raises(UpdateError, match="not a valid SHA-256"):
            updater.apply_frozen_update(
                _exe_release(), exe_path=str(tmp_path / "x.exe"),
                session=FakeDownloads(self.PAYLOAD, "not-a-hash"),
                run=self._passes_selfcheck)

    def test_the_swap_script_waits_swaps_restores_and_relaunches(self, tmp_path):
        script = updater.write_swap_script(
            r"C:\Apps\ProviderLeadGen.exe", r"C:\Apps\ProviderLeadGen.exe.new",
            pid=4242, updated_from="2.0.0", directory=str(tmp_path))
        body = open(script, encoding="utf-8").read()
        assert 'PID eq 4242' in body, "must wait for the running copy to exit"
        assert r'move /Y "C:\Apps\ProviderLeadGen.exe" "C:\Apps\ProviderLeadGen.exe.old"' in body
        assert r'move /Y "C:\Apps\ProviderLeadGen.exe.new" "C:\Apps\ProviderLeadGen.exe"' in body
        assert "if errorlevel 1 move" in body, "a failed swap must restore the old exe"
        assert "--updated-from 2.0.0" in body


class TestRestart:

    def test_a_checkout_restarts_the_app_with_where_it_came_from(self, tmp_path):
        launched = []
        updater.restart("2.0.0", app_path=str(tmp_path / "app.py"),
                        cwd=str(tmp_path),
                        popen=lambda command, **kw: launched.append((command, kw)))
        command, kwargs = launched[0]
        assert command[-3:] == [str(tmp_path / "app.py"), "--updated-from", "2.0.0"]
        assert kwargs["cwd"] == str(tmp_path)

    def test_the_exe_restarts_through_its_swap_script(self, tmp_path):
        launched = []
        updater.restart("2.0.0", swap_script=r"C:\Temp\swap.bat",
                        popen=lambda command, **kw: launched.append(command))
        assert launched[0] == ["cmd", "/c", r"C:\Temp\swap.bat"]
