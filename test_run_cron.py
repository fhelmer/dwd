import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
RUN_CRON_SCRIPT = REPO_ROOT / "run_cron.sh"


def test_run_cron_help():
    result = subprocess.run(
        [str(RUN_CRON_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Starting dwd_grib_downloader.py --help" in result.stdout
    assert "Usage: dwd_grib_downloader.py" in result.stdout
    assert "Finished with exit code 0" in result.stdout


def test_run_cron_from_different_directory(tmp_path):
    result = subprocess.run(
        [str(RUN_CRON_SCRIPT), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Starting dwd_grib_downloader.py --help" in result.stdout
    assert "Finished with exit code 0" in result.stdout


def test_run_cron_lock_collision(tmp_path):
    lock_file = tmp_path / "test_cron.lock"
    # Open and lock the file with flock in a background process or via flock command
    flock_proc = subprocess.Popen(
        ["flock", "-x", str(lock_file), "sleep", "2"],
    )
    try:
        env = os.environ.copy()
        env["CRON_LOCK_FILE"] = str(lock_file)
        result = subprocess.run(
            [str(RUN_CRON_SCRIPT), "--help"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert "Another instance is already running" in result.stdout
    finally:
        flock_proc.kill()
        flock_proc.wait()


def test_run_cron_propagates_exit_code():
    result = subprocess.run(
        [str(RUN_CRON_SCRIPT), "--invalid-option-test"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert f"Finished with exit code {result.returncode}" in result.stdout
