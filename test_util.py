import subprocess

import pytest

import util


@pytest.fixture(autouse=True)
def reset_dryrun(monkeypatch):
    monkeypatch.setattr(util, "dryrun", False)


def test_run_command_captures_stdout_and_code():
    result = util.run_command("echo hello")

    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.code == 0


def test_run_command_captures_stderr():
    result = util.run_command("echo oops 1>&2")

    assert result.stderr == "oops\n"


def test_run_command_uses_cwd(tmp_path):
    result = util.run_command("pwd", cwd=str(tmp_path))

    assert result.stdout.strip() == str(tmp_path)


def test_run_command_supports_shell_globs(tmp_path):
    (tmp_path / "a.bz2").touch()
    (tmp_path / "b.bz2").touch()

    result = util.run_command("ls *.bz2", cwd=str(tmp_path))

    assert result.stdout.split() == ["a.bz2", "b.bz2"]


def test_run_command_exits_on_failure():
    with pytest.raises(SystemExit) as exc:
        util.run_command("exit 3")

    assert exc.value.code == 1


def test_run_command_dryrun_does_not_execute(monkeypatch, tmp_path):
    monkeypatch.setattr(util, "dryrun", True)

    def fail(*args, **kwargs):
        raise AssertionError("subprocess should not be called in dryrun mode")

    monkeypatch.setattr(subprocess, "Popen", fail)

    assert util.run_command(f"touch {tmp_path}/x") == ""
    assert not (tmp_path / "x").exists()
