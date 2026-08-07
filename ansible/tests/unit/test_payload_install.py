"""Lock down roles/payload -- the host engine install.

The role exists to close a real ordering hole: the engines used to arrive from
roles/catena-admin (site.yml position 13) while roles/cloudflare_tunnel
(position 9) dispatches one of them, so a first converge on a host that already
held a Cloudflare token deferred the tunnel and roles/oauth2_proxy then waited
on an edge nobody had brought up. These tests pin the two things that keep that
closed: the role's POSITION in site.yml, and the marker semantics that make a
re-converge a no-op without freezing an image upgrade out.

Run: uv run pytest tests/unit/test_payload_install.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "roles" / "payload"
DEFAULTS = ROLE / "defaults" / "main.yml"
TASKS = ROLE / "tasks" / "main.yml"
SITE = ANSIBLE / "playbooks" / "site.yml"
CF_TASKS = ANSIBLE / "roles" / "cloudflare_tunnel" / "tasks" / "main.yml"


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _role_order() -> list[str]:
    """Role names in site.yml play order."""
    play = yaml.safe_load(SITE.read_text())[0]
    return [r["role"] if isinstance(r, dict) else r for r in play["roles"]]


def _as_list(cond) -> list:
    if cond is None:
        return []
    return list(cond) if isinstance(cond, list) else [cond]


def _flatten(tasks: list[dict], inherited: list | None = None) -> list[dict]:
    """Flatten blocks, pushing each block's `when` down onto its children.

    Ansible applies a block-level condition to every task inside it (including
    `always`), so a test that reads a bare task's `when` sees only half the
    gate. Flattening without that inheritance is how a gated task reads as
    ungated.
    """
    inherited = inherited or []
    out: list[dict] = []
    for t in tasks:
        own = inherited + _as_list(t.get("when"))
        merged = dict(t)
        if own:
            merged["when"] = own
        out.append(merged)
        for key in ("block", "always", "rescue"):
            if isinstance(t.get(key), list):
                out.extend(_flatten(t[key], own))
    return out


# --- position ---------------------------------------------------------------
def test_payload_runs_after_docker_and_before_every_engine_consumer():
    order = _role_order()
    assert "payload" in order, "roles/payload is not in site.yml"
    pos = order.index("payload")
    # docker is all it needs, and it needs docker.
    assert order.index("docker") < pos
    # Everything that dispatches an engine, or depends on one having run, must
    # come after. cloudflare_tunnel is the one that broke.
    for later in ("cloudflare_tunnel", "keycloak", "oauth2_proxy", "catena-admin"):
        assert pos < order.index(later), f"{later} runs before roles/payload"


def test_payload_precedes_the_roles_that_wait_on_the_edge():
    """The ordering this role exists to guarantee, stated as the chain."""
    order = _role_order()
    assert (
        order.index("payload")
        < order.index("cloudflare_tunnel")
        < order.index("oauth2_proxy")
    )


# --- what it installs from --------------------------------------------------
def test_image_defaults_to_the_catena_admin_image():
    d = _defaults()
    assert "catena_admin_image" in d["catena_payload_image"]


def test_extraction_reads_the_image_payload_path():
    d = _defaults()
    assert d["catena_payload_image_path"] == "/usr/local/share/catena-ee"
    assert "catena_payload_image_path" in TASKS.read_text()


def test_install_and_pull_are_env_overridable():
    """The bench builds its own image on the VPS and must never pull :latest."""
    d = _defaults()
    assert "CATENA_PAYLOAD_INSTALL" in d["catena_payload_install"]
    assert "CATENA_PAYLOAD_PULL" in d["catena_payload_pull"]
    assert "default='true'" in d["catena_payload_install"]
    assert "default='true'" in d["catena_payload_pull"]


# --- marker semantics -------------------------------------------------------
def test_marker_holds_the_image_id_not_a_flag():
    """Keyed on the image ID so an upgrade reinstalls and a re-converge does not.

    A bare "installed" flag would freeze the host on its first image forever; an
    unconditional install would replace a deliberately build-STAMPED engine
    (activate_ee ships one) with an unstamped build.
    """
    names = [t.get("name", "") for t in _flatten(_tasks())]
    assert any("record the image the engines came from" in n for n in names)
    text = TASKS.read_text()
    assert "_payload_image_id.stdout" in text
    assert "catena_payload_marker" in text


def test_reconverge_against_the_same_image_installs_nothing():
    text = TASKS.read_text()
    # Every mutating task is gated on the marker NOT matching.
    for task in _flatten(_tasks()):
        name = task.get("name", "")
        if any(
            k in name
            for k in (
                "docker cp the payload tree",
                "install the engines onto the host",
                "record the image the engines came from",
            )
        ):
            cond = task.get("when")
            cond = cond if isinstance(cond, list) else [cond]
            assert any(
                "_payload_current" in str(c) for c in cond
            ), f"{name!r} is not gated on the marker"
    assert "_payload_current" in text


def test_throwaway_container_is_removed_even_on_failure():
    """A created-but-never-started container still holds a writable layer."""
    for task in _flatten(_tasks()):
        if "always" in task:
            names = [t.get("name", "") for t in task["always"]]
            if any("remove the throwaway container" in n for n in names):
                return
    raise AssertionError("container removal is not in an `always` block")


def test_ownership_is_captured_before_the_copy_and_restored_after():
    """docker cp writes as root; the container syncs here as its nonroot uid."""
    names = [t.get("name", "") for t in _flatten(_tasks())]
    capture = next(i for i, n in enumerate(names) if "capture the extraction" in n)
    copy = next(i for i, n in enumerate(names) if "docker cp the payload tree" in n)
    restore = next(i for i, n in enumerate(names) if "restore the extraction" in n)
    assert capture < copy < restore


# --- the hand-off to cloudflare_tunnel --------------------------------------
def test_role_publishes_whether_the_converge_owns_the_engines():
    text = TASKS.read_text()
    assert "catena_payload_engines_expected" in text


def test_cloudflare_tunnel_fails_hard_when_the_converge_owns_the_engines():
    """Deferring silently is what produced the oauth2_proxy failure two roles on."""
    tasks = _flatten(yaml.safe_load(CF_TASKS.read_text()))
    fail = [t for t in tasks if "ansible.builtin.fail" in t]
    assert fail, "no hard stop for a missing engine"
    cond = " ".join(str(c) for c in fail[0]["when"])
    assert "catena_payload_engines_expected" in cond
    assert "_cf_sync_bin" in cond


def test_cloudflare_tunnel_still_defers_when_engines_are_staged_out_of_band():
    tasks = _flatten(yaml.safe_load(CF_TASKS.read_text()))
    deferred = [
        t for t in tasks if "staged out of band" in t.get("name", "")
    ]
    assert deferred, "the out-of-band staging path lost its deferral"
    cond = " ".join(str(c) for c in deferred[0]["when"])
    assert "not (catena_payload_engines_expected" in cond


# --- digest gate ------------------------------------------------------------
# install-ee-payload.sh puts Go binaries into /usr/local/bin and runs as root,
# so whatever the image resolves to owns the box. A moved tag or a compromised
# registry account is the threat; a digest check is the verification that works
# offline and needs nothing on the host.


def _names(tasks: list[dict]) -> list[str]:
    return [t.get("name", "") for t in tasks]


def _index_of(tasks: list[dict], needle: str) -> int:
    for i, name in enumerate(_names(tasks)):
        if needle in name:
            return i
    raise AssertionError(f"no task matching {needle!r}; have {_names(tasks)}")


def test_digest_mismatch_fails_before_anything_is_extracted_or_run() -> None:
    """Ordering IS the security property. A mismatch that fails after
    `docker create` has already copied the tree, or after
    install-ee-payload.sh has run, has verified nothing -- the code is on the
    host and executed by then."""
    flat = _flatten(_tasks())
    fail_at = _index_of(flat, "not the pinned one")

    for later in ("create a throwaway container",
                  "docker cp the payload tree",
                  "install the engines onto the host"):
        assert fail_at < _index_of(flat, later), (
            f"the digest check runs AFTER {later!r}, so a wrong image is "
            f"already on the host by the time it is refused"
        )


def test_digest_gate_is_conditional_on_a_pin_being_set() -> None:
    flat = _flatten(_tasks())
    fail_task = flat[_index_of(flat, "not the pinned one")]
    conds = " ".join(str(c) for c in _as_list(fail_task.get("when")))
    assert "catena_payload_image_digest | length > 0" in conds
    assert "catena_payload_image_digest not in" in conds


def test_an_unpinned_image_says_so_out_loud() -> None:
    """Empty is "unchecked", not "verified". A silent skip reads exactly like
    a passing verification, which is the failure this gate exists to avoid."""
    flat = _flatten(_tasks())
    notice = flat[_index_of(flat, "digest is NOT pinned")]
    conds = " ".join(str(c) for c in _as_list(notice.get("when")))
    assert "catena_payload_image_digest | length == 0" in conds
    assert "WITHOUT a digest check" in notice["ansible.builtin.debug"]["msg"]


def test_digest_is_env_overridable_and_defaults_to_empty() -> None:
    """Empty by default: no image is published yet, and a default that
    pretended otherwise would fail every converge."""
    raw = _defaults()["catena_payload_image_digest"]
    assert "CATENA_PAYLOAD_IMAGE_DIGEST" in raw
    assert "default=''" in raw
