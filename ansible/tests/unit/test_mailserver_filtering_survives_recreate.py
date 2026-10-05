"""rspamd filtering comes back with a new dms container, not with the next converge.

/etc/rspamd is container filesystem. A new dms task (node reboot, swarm
reschedule, image update, restore) starts from the image defaults: antivirus
off, stock thresholds. docker-mailserver copies only rspamd/override.d from
the mail-config volume at start, never local.d, and a converge that finds the
drift reloads rspamd, the one change install_rerun's settle stage does not allow.
docker-mailserver runs /tmp/docker-mailserver/user-patches.sh once per new
container, after its rspamd setup and before rspamd starts, which is where the
drop-ins are re-applied and where rspamd waits for clamd's name to resolve.

Run: uv run pytest tests/unit/test_mailserver_filtering_survives_recreate.py
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import jinja2
import yaml

TASKS = (Path(__file__).resolve().parents[2]
         / "reconcile" / "roles" / "infrastructure" / "tasks")


def _block() -> list[dict]:
    tasks = yaml.safe_load((TASKS / "mailserver_filtering.yml").read_text())
    return next(t for t in tasks if isinstance(t, dict) and "block" in t)["block"]


def _task(prefix: str) -> dict:
    return next(t for t in _block() if t["name"].startswith(prefix))


def test_the_hook_copies_exactly_the_staged_drop_ins():
    content = _task("Mailserver filtering: render the dms start hook")[
        "ansible.builtin.copy"]["content"]
    confs = [{"name": "antivirus.conf"}, {"name": "rbl.conf"}]
    rendered = jinja2.Template(content).render(_ms_rspamd_confs=confs)
    cps = [ln for ln in rendered.splitlines() if ln.startswith("cp ")]
    assert cps == [
        "cp /tmp/docker-mailserver/rspamd/local.d/antivirus.conf "
        "/etc/rspamd/local.d/antivirus.conf",
        "cp /tmp/docker-mailserver/rspamd/local.d/rbl.conf "
        "/etc/rspamd/local.d/rbl.conf",
    ]
    assert rendered.startswith("#!/bin/bash\n")


def test_the_hook_lands_where_docker_mailserver_runs_it():
    argv = _task("Mailserver filtering: stage the start hook")[
        "ansible.builtin.command"]["argv"]
    assert argv[:2] == ["/usr/local/bin/catena-dms-exec", "cp"]
    assert argv[-1] == "/tmp/docker-mailserver/user-patches.sh"


def test_the_hook_is_staged_after_the_drop_ins_it_copies():
    names = [t["name"] for t in _block()]
    staged = names.index(
        "Mailserver filtering: stage drop-ins into the mail-config volume")
    hook = next(i for i, n in enumerate(names)
                if n.startswith("Mailserver filtering: stage the start hook"))
    assert staged < hook


def _run_hook(tmp_path: Path, resolves_after: int) -> tuple[int, str, int]:
    """Run the rendered hook with stub commands; getent fails `resolves_after`
    times and then answers. Returns (rc, stderr, getent calls)."""
    content = _task("Mailserver filtering: render the dms start hook")[
        "ansible.builtin.copy"]["content"]
    rendered = jinja2.Template(content).render(
        _ms_rspamd_confs=[{"name": "antivirus.conf"}])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("mkdir", "cp", "sleep"):
        (bin_dir / name).write_text("#!/bin/bash\nexit 0\n")
        (bin_dir / name).chmod(0o755)
    calls = tmp_path / "getent-calls"
    calls.write_text("0\n")
    (bin_dir / "getent").write_text(
        "#!/bin/bash\n"
        f"n=$(< {calls}); n=$((n + 1)); echo \"$n\" > {calls}\n"
        f"[ \"$n\" -gt {resolves_after} ]\n")
    (bin_dir / "getent").chmod(0o755)
    hook = tmp_path / "user-patches.sh"
    hook.write_text(rendered)
    r = subprocess.run(["/bin/bash", str(hook)], env={"PATH": str(bin_dir)},
                       capture_output=True, text=True, timeout=30, check=False)
    return r.returncode, r.stderr, int(calls.read_text())


def test_rspamd_waits_for_clamav_to_resolve(tmp_path):
    """rspamd resolves clamav once, at config load, and drops the antivirus
    rule for the life of the process when it cannot. After a rewind the dms
    container started 15s before the clamav task:

        rspamd_resolve_addrs: address resolution for clamav failed
        antivirus.lua:206: cannot add rule: "clamav"

    and EICAR went through unscanned, with no CLAM_VIRUS_FAIL to reject on."""
    rc, err, calls = _run_hook(tmp_path, resolves_after=3)
    assert (rc, calls) == (0, 4)
    assert err == ""


def test_the_wait_for_clamav_is_bounded(tmp_path):
    rc, err, calls = _run_hook(tmp_path, resolves_after=10_000)
    assert rc == 0
    assert calls == 61
    assert "clamav did not resolve within 120s" in err
