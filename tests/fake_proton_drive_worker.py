#!/usr/bin/env python3
"""Stand-in for proton-drive-worker, speaking its JSON-lines protocol.

Each request runs tests/fake_proton_drive.py, in a thread so requests overlap
like they do in the real worker. Every request is appended to
$FAKE_DRIVE_ROOT/worker.log as {"pid", "argv"}. With $FAKE_WORKER_EXIT_AFTER=n
it exits without replying once it has answered n requests (0: never answers
anything).
"""

import json
import os
import subprocess
import sys
import threading

FAKE_CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_proton_drive.py")
ROOT = os.environ["FAKE_DRIVE_ROOT"]
EXIT_AFTER = os.environ.get("FAKE_WORKER_EXIT_AFTER")

lock = threading.Lock()
answered = 0


def handle(request):
    global answered
    proc = subprocess.run([sys.executable, FAKE_CLI, *request["argv"]], capture_output=True, text=True)
    if proc.returncode == 0:
        reply = {"id": request["id"], "ok": True, "stdout": proc.stdout}
    else:
        reply = {"id": request["id"], "ok": False, "error": proc.stderr.strip().splitlines()[-1]}
    with lock:
        if EXIT_AFTER is not None and answered >= int(EXIT_AFTER):
            os._exit(1)
        answered += 1
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


def main():
    for line in sys.stdin:
        request = json.loads(line)
        with open(os.path.join(ROOT, "worker.log"), "a") as log:
            log.write(json.dumps({"pid": os.getpid(), "argv": request["argv"]}) + "\n")
        threading.Thread(target=handle, args=(request,)).start()


if __name__ == "__main__":
    main()
