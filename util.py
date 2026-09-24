import logging
import subprocess
import sys

LOG = logging.getLogger()
dryrun = False

class RunResult:
    def __init__(self):
        pass

def run_command(cmd, cwd=None):
    result = ""
    if dryrun:
        LOG.warning("dryrun command:%s", cmd)
    else:
        LOG.info ("Running command:" + cmd)
        proc = subprocess.Popen(cmd, shell=True, stdout = subprocess.PIPE,
                                stderr = subprocess.PIPE,
                                cwd = cwd,
                                universal_newlines=True)
        result = RunResult()
        result.stdout, result.stderr = proc.communicate()
        LOG.info ("Result stdout: " + result.stdout + "Result stderr: '" + result.stderr + "'")
        result.code = proc.wait()
        if result.code != 0:
            LOG.error ("Return code != 0 exiting. Code = " + str(result.code))
            LOG.error ("Result stdout: " + result.stdout + "Result stderr: " + result.stderr)
            sys.exit(1)
    return result
