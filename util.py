import logging
import subprocess
import sys

LOG = logging.getLogger(__name__)
dryrun = False

class RunResult:
    def __init__(self, stdout: str = "", stderr: str = "", code: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.code = code

def run_command(cmd, cwd=None):
    if dryrun:
        LOG.warning("dryrun command: %s", cmd)
        return ""

    LOG.info("Running command: %s", cmd)
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        text=True,
    )
    stdout, stderr = proc.communicate()
    result = RunResult(stdout=stdout, stderr=stderr, code=proc.returncode)
    LOG.info("Result stdout: %s Result stderr: '%s'", result.stdout, result.stderr)
    if result.code != 0:
        LOG.error("Return code != 0 exiting. Code = %s", result.code)
        LOG.error("Result stdout: %s Result stderr: %s", result.stdout, result.stderr)
        sys.exit(1)
    return result

