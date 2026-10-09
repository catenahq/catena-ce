"""Lock down reconcile/roles/payload -- the host engine install.

The role closes a real ordering hole. reconcile/roles/cloudflare_tunnel
dispatches one of the engines, so engines arriving any later -- from
reconcile/roles/catena-admin, which runs after it, say -- leave a first
converge on a host that already holds a Cloudflare token with no tunnel, and
reconcile/roles/oauth2_proxy then waits on an edge nobody brought up. These tests
pin the two things that keep it closed: the role's POSITION in converge.yml, and the
marker semantics that make a re-converge a no-op without freezing an image
upgrade out.

Run: uv run pytest tests/unit/test_payload_install.py
"""
from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
ROLE = ANSIBLE / "reconcile" / "roles" / "payload"
DEFAULTS = ROLE / "defaults" / "main.yml"
TASKS = ROLE / "tasks" / "main.yml"
SITE = ANSIBLE / "playbooks" / "converge.yml"
CF_TASKS = ANSIBLE / "reconcile" / "roles" / "cloudflare_tunnel" / "tasks" / "main.yml"


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _tasks() -> list[dict]:
    return yaml.safe_load(TASKS.read_text())


def _role_order() -> list[str]:
    """Role names in converge.yml play order."""
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
    assert "payload" in order, "reconcile/roles/payload is not in converge.yml"
    pos = order.index("payload")
    # docker is all it needs, and it needs docker.
    assert order.index("docker") < pos
    # Everything that dispatches an engine, or depends on one having run, must
    # come after. cloudflare_tunnel is the one that broke.
    for later in ("cloudflare_tunnel", "keycloak", "oauth2_proxy", "catena-admin"):
        assert pos < order.index(later), f"{later} runs before reconcile/roles/payload"


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


def test_the_engines_follow_the_running_shell():
    """One version input per host: the service spec, which
    reconcile/roles/catena-admin reconciles to catena_admin_image later in the
    converge. Following it leaves the engines no second answer to disagree
    with the shell the host runs.
    """
    flat = _flatten(_tasks())
    inspect = flat[_index_of(flat, "what image is the catena-admin service")]
    argv = inspect["ansible.builtin.command"]["argv"]
    assert "service" in argv and "inspect" in argv
    assert any("ContainerSpec.Image" in str(a) for a in argv)
    assert inspect.get("failed_when") is False, (
        "no such service is the normal first-converge answer, not an error")

    follow = flat[_index_of(flat, "the engines follow the shell")]
    assert (follow["ansible.builtin.set_fact"]["catena_payload_image"]
            == "{{ _payload_service_image.stdout | trim }}")


def test_following_the_shell_happens_before_the_image_is_resolved():
    """Resolving the ID, gating the digest or pulling before the source is
    settled would all act on the fallback."""
    flat = _flatten(_tasks())
    follow = _index_of(flat, "the engines follow the shell")
    for later in ("pull", "resolve the image ID", "not the pinned one",
                  "docker cp the payload tree"):
        assert follow < _index_of(flat, later), f"{later!r} runs first"


def test_an_explicit_image_override_still_wins():
    """install-host.sh points CATENA_PAYLOAD_IMAGE at the release it installs.
    A service-derived value that overrode it would install the engines of the
    release being replaced under the new release's tree.
    """
    flat = _flatten(_tasks())
    follow = flat[_index_of(flat, "the engines follow the shell")]
    conds = " ".join(str(c) for c in _as_list(follow.get("when")))
    assert "CATENA_PAYLOAD_IMAGE" in conds
    assert "length == 0" in conds


def test_having_no_service_to_follow_says_so_out_loud():
    """Silence here reads as "followed the shell" on a host where nothing was
    followed -- the same shape as the unchecked-digest skip."""
    flat = _flatten(_tasks())
    notice = flat[_index_of(flat, "nothing to follow")]
    conds = " ".join(str(c) for c in _as_list(notice.get("when")))
    assert "_payload_from_service" in conds


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


def test_a_digest_pinned_image_already_here_is_not_pulled_again():
    """The host's own converge names the image the panel runs, which is on the
    host by definition. Pulling it anyway made every on-host converge fail
    while the registry did not answer."""
    flat = _flatten(_tasks())
    present = flat[_index_of(flat, "digest-pinned image already on this host")]
    conds = " ".join(str(c) for c in _as_list(present.get("when")))
    assert "'@sha256:' in catena_payload_image" in conds
    assert present.get("failed_when") is False
    pull = flat[_index_of(flat, "Payload: pull")]
    assert "(_payload_present.rc | default(1)) != 0" in _as_list(pull.get("when"))
    assert _index_of(flat, "already on this host") < _index_of(flat, "Payload: pull")


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
    assert "catena_payload_expected" in cond
    assert "catena_payload_missing" in cond


def test_cloudflare_tunnel_still_defers_when_engines_are_staged_out_of_band():
    tasks = _flatten(yaml.safe_load(CF_TASKS.read_text()))
    deferred = [
        t for t in tasks if "staged out of band" in t.get("name", "")
    ]
    assert deferred, "the out-of-band staging path lost its deferral"
    cond = " ".join(str(c) for c in deferred[0]["when"])
    assert "not (catena_payload_expected" in cond


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


# --- pairing gate -----------------------------------------------------------
# The roles running a converge and the engines it installs must come from one
# catena-ce tree, or a file one version hands to the other is left with no
# owner, and the next converge fails on its absence.


def test_a_tree_the_image_was_not_built_with_is_refused_before_anything_is_installed() -> None:
    flat = _flatten(_tasks())
    refuse = _index_of(flat, "not one version -- refusing")
    assert _index_of(flat, "create a throwaway container") < refuse
    for later in ("docker cp the payload tree",
                  "install the engines onto the host",
                  "record the image the engines came from"):
        assert refuse < _index_of(flat, later), f"{later!r} runs before the refusal"


def test_the_pairing_gate_runs_on_every_converge_not_only_on_an_extract() -> None:
    """A re-converge whose engines are current still runs this tree's roles
    over them, so it is checked whether or not anything is copied out."""
    flat = _flatten(_tasks())
    for needle in ("copy out the record of the tree",
                   "name the tree this converge runs",
                   "not one version -- refusing"):
        conds = " ".join(str(c) for c in _as_list(flat[_index_of(flat, needle)].get("when")))
        assert "_payload_current" not in conds, needle
    assert _index_of(flat, "not one version -- refusing") < _index_of(
        flat, "decide whether the installed engines are current")


def test_both_sides_are_named_by_the_one_helper_and_an_unrecorded_image_is_refused() -> None:
    flat = _flatten(_tasks())
    name = flat[_index_of(flat, "name the tree this converge runs")]
    assert "helpers/tree_hash.py" in name["ansible.builtin.set_fact"]["_payload_tree"]
    refuse = flat[_index_of(flat, "not one version -- refusing")]
    cond = " ".join(str(c) for c in _as_list(refuse.get("when")))
    assert "_payload_record.tree_sha256 | default('', true) | length == 0" in cond
    assert "_payload_record.tree_sha256 != _payload_tree.tree_sha256" in cond
    assert _defaults()["catena_payload_tree_record"] == "/usr/local/share/catena-ce/VENDOR.json"


def test_the_copied_record_is_removed_even_on_failure() -> None:
    for task in _flatten(_tasks()):
        if "always" in task:
            names = [t.get("name", "") for t in task["always"]]
            if any("remove the copied tree record" in n for n in names):
                return
    raise AssertionError("the tree record copy is not removed in an `always` block")


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


def test_a_checked_image_says_so_too() -> None:
    """Both outcomes are named in the converge's output, so a reader of it can
    tell the check ran. Placed after the refusal, which stops the play on a
    mismatch, and before anything is copied out."""
    flat = _flatten(_tasks())
    notice = flat[_index_of(flat, "digest matches the pin")]
    conds = " ".join(str(c) for c in _as_list(notice.get("when")))
    assert "catena_payload_image_digest | length > 0" in conds
    assert "digest check passed" in notice["ansible.builtin.debug"]["msg"]
    assert (_index_of(flat, "not the pinned one") < _index_of(flat, "digest matches the pin")
            < _index_of(flat, "create a throwaway container"))


def _render_digest(admin_image: str, env_digest: str = "") -> str:
    """catena_payload_image_digest rendered under a fake context. A substring
    check cannot tell a pin from one that never matches, and "never matches"
    reads as an unchecked extract, which looks exactly like a passing one."""
    raw = _defaults()["catena_payload_image_digest"]
    asked = []

    def lookup(kind, name, default=""):
        asked.append((kind, name))
        return env_digest or default

    out = jinja2.Environment().from_string(raw).render(
        lookup=lookup, catena_admin_image=admin_image).strip()
    assert asked == [("env", "CATENA_PAYLOAD_IMAGE_DIGEST")], asked
    return out


def test_every_install_and_host_converge_arms_the_digest_gate() -> None:
    """The pin is the digest of the image the converge was started with:
    the release install-host.sh fetched, `repo:tag@sha256:...`, or the image
    the panel service runs, which catena-converge names by digest."""
    digest = "sha256:" + "ab" * 32
    for image in (f"ghcr.io/catenahq/catena-admin:v0.6.2@{digest}",
                  f"10.0.0.1:5001/catena-admin:bench-3@{digest}",
                  f"ghcr.io/catenahq/catena-admin@{digest}"):
        assert _render_digest(image) == digest, image


def test_an_image_named_by_tag_alone_is_extracted_unchecked() -> None:
    """No digest to hold it to; the role says so out loud rather than failing
    closed on a host whose image was named without one."""
    assert _render_digest("ghcr.io/catenahq/catena-admin:v0.6.2") == ""
    assert _render_digest("") == ""


def test_an_explicit_digest_wins() -> None:
    """CATENA_PAYLOAD_IMAGE_DIGEST is how the fail-closed leg is exercised: a
    deliberately wrong value must replace the image's own digest."""
    wrong = "sha256:" + "0" * 64
    image = "ghcr.io/catenahq/catena-admin:v0.6.2@sha256:" + "ab" * 32
    assert _render_digest(image, env_digest=wrong) == wrong
    assert _render_digest("ghcr.io/catenahq/catena-admin:v0.6.2",
                          env_digest=wrong) == wrong
