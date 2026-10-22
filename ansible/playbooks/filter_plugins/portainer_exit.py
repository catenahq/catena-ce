"""Ansible filter for reconcile/roles/portainer_exit: may the Portainer on this
host be removed?

`catena-apps adopt` gives every client stack Portainer holds an application
folder and prints a JSON report {"adopted": [...], "kept": [...], "refused":
[{"stack": ..., "reason": ...}]}. A stack in `refused` is still deployed by
Portainer, so Portainer goes only when the pass ran, exited 0 and refused
nothing. Any other outcome keeps Portainer and says why.

End-to-end coverage:
    catena-ce ansible/tests/unit/test_portainer_exit.py
"""

from __future__ import annotations

import json


def portainer_exit_verdict(adopt):
    """The verdict on a registered `catena-apps adopt` result.

    Returns {"remove": bool, "staying": [stack names], "why": str}. `why` is a
    sentence naming what keeps Portainer, empty when it may go. A skipped or
    missing result keeps Portainer: nothing was adopted, so nothing is known.
    """
    if not isinstance(adopt, dict) or adopt.get("skipped"):
        return {"remove": False, "staying": [],
                "why": "catena-apps adopt did not run on this host"}
    rc = adopt.get("rc")
    if rc != 0:
        stderr = " | ".join(adopt.get("stderr_lines") or []) or str(adopt.get("stderr") or "").strip()
        return {"remove": False, "staying": [],
                "why": f"catena-apps adopt exited {rc}: {stderr or 'no message'}"}
    try:
        report = json.loads(adopt.get("stdout") or "")
        refused = report["refused"]
        staying = [r["stack"] for r in refused]
        reasons = [f'{r["stack"]}: {r["reason"]}' for r in refused]
    except (ValueError, KeyError, TypeError):
        return {"remove": False, "staying": [],
                "why": "catena-apps adopt printed a report that cannot be read"}
    if staying:
        return {"remove": False, "staying": staying,
                "why": "stacks still deployed by Portainer: " + "; ".join(reasons)}
    return {"remove": True, "staying": [], "why": ""}


class FilterModule:
    def filters(self):
        return {
            "portainer_exit_verdict": portainer_exit_verdict,
        }
