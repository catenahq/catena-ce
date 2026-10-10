"""Every task that handles a secret runs under no_log: true, and no secret
travels through a task's environment (CV7).

What a task is given comes back in its result: a command's argv as `cmd`, a
loop's items, the facts a set_fact sets, an API's answer to a call that
carried a token. A failed task prints its result and -v prints every one.
no_log: true on the task, or on a block or play around it, puts a censored
notice in the result's place. The check reads a task's module arguments,
`args`, `environment`, loop, loop label and `vars`.

Under no_log a failed task still prints its error message, a refused uri
call's status line among them. A command whose stderr explains its failure
registers it with failed_when: false and prints it in a task of its own, as
reconcile/roles/keycloak/tasks/_config_cli_import.yml does.

A task's or a block's `environment` rides the module's `/bin/sh -c` command
line, which -vvv and `ps` show whatever no_log says, so it holds no secret. A
secret goes in on stdin, as test_keycloak_logins_read_the_password_on_stdin.py
and test_seeds_read_credentials_on_stdin.py require of theirs, or through a
helper that reads it on the host, as reconcile/roles/backup runs restic under
catena-restic-env.

A secret is a variable that is
- a key the on-box store declares (helpers/onbox_config.py secret_names);
- named as one: its name ends in password, passwd, secret(s), token(s),
  api_key, apikey, auth_key, authkey, private_key, preauth_key, restic_env or
  keyset;
- a role default, role var or group variable whose value references a secret;
- the registered result of a slurp of a secret file: the store, or a path
  that names a key, password, secret, token or env file.

A reference that tests presence alone (`x | length`, `(x | default('')) |
length`, `x is defined`) carries no value out, and an assert's `that` reports
the expression's text. An include's `vars` are evaluated for the tasks it
includes, which are checked in their own file.

ALLOWED names each task that references a secret and prints none of it, with
the reason.

Run: uv run pytest tests/unit/test_every_secret_bearing_task_sets_no_log.py
"""
from __future__ import annotations

import importlib.util
import re

import yaml

from ansible_tree import ANSIBLE

_ROOTS = ("playbooks", "bootstrap", "reconcile")
_VAR_DIRS = {"defaults", "vars", "group_vars"}
_NON_TASK_DIRS = _VAR_DIRS | {"templates", "files", "meta"}

_SECRET_NAME = re.compile(
    r"(?:^|_)(?:password|passwd|secrets?|tokens?|api_?key|auth_?key|private_key|"
    r"preauth_key|restic_env|keyset)$")
_SECRET_FILE = re.compile(
    r"config\.json|onbox_config|[-_.](?:key|pass|password|secret|token|env)(?![a-z])")

# The name, which no_log leaves on screen, and the conditions and flags, whose
# values a run never prints.
_CONTROL = {"name", "when", "changed_when", "failed_when", "until", "register",
            "tags", "notify", "listen", "retries", "delay", "become",
            "become_user", "delegate_to", "run_once", "ignore_errors", "no_log",
            "check_mode", "timeout"}
_INCLUDES = {f"{prefix}{verb}" for prefix in ("", "ansible.builtin.")
             for verb in ("include_tasks", "import_tasks", "include_role", "import_role")}
_ASSERTS = {"assert", "ansible.builtin.assert"}
_DEBUGS = {"debug", "ansible.builtin.debug"}
_SLURPS = {"slurp", "ansible.builtin.slurp"}

_JINJA = re.compile(r"{{(.*?)}}|{%(.*?)%}", re.S)
_STRING = re.compile(r"'[^']*'|\"[^\"]*\"")
_PRESENCE = re.compile(
    r"\(\s*[\w.]+\s*(?:\|\s*default\(\s*\)\s*)?\)\s*\|\s*length\b"
    r"|[\w.]+\s*(?:\|\s*default\(\s*\)\s*)?\|\s*length\b"
    r"|[\w.]+\s+is\s+(?:not\s+)?(?:defined|undefined|none)\b")
_IDENT = re.compile(r"[A-Za-z_]\w*")

# (file, task name) -> why the task prints no secret although it references one.
ALLOWED = {
    ("reconcile/roles/catena-admin/tasks/deploy.yml",
     "Catena-admin: the reconcile has to have settled the drift it applied"):
        "catena_admin_secret_drift reads the secrets' names and targets alone, "
        "and its fail_msg, the reason the task exists, prints the drift flags",
}


def _expressions(node):
    if isinstance(node, str):
        for match in _JINJA.finditer(node):
            yield match.group(1) or match.group(2)
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _expressions(key)
            yield from _expressions(value)
    elif isinstance(node, list):
        for value in node:
            yield from _expressions(value)


def _is_secret(name: str, secrets: set[str]) -> bool:
    return name in secrets or bool(_SECRET_NAME.search(name))


def _references(expressions, secrets: set[str]) -> set[str]:
    out = set()
    for expression in expressions:
        expression = _PRESENCE.sub("", _STRING.sub("", expression))
        out |= {name for name in _IDENT.findall(expression) if _is_secret(name, secrets)}
    return out


def _task_references(task: dict, secrets: set[str]) -> set[str]:
    """The secrets a task's printed keys reference."""
    include = any(key in task for key in _INCLUDES)
    expressions = []
    for key, value in task.items():
        if key in _CONTROL or key in ("block", "rescue", "always") \
                or (include and key == "vars"):
            continue
        if key in _ASSERTS and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k != "that"}
        if key in _DEBUGS and isinstance(value, dict) and "var" in value:
            expressions.append(str(value["var"]))
        expressions.extend(_expressions(value))
    return _references(expressions, secrets)


def _walk(items, no_log):
    """Every task and block with the no_log it runs under: its own, else the
    nearest block's or play's."""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        own = item.get("no_log", no_log)
        yield item, own
        for key in ("block", "rescue", "always"):
            yield from _walk(item.get(key), own)


def _files(var_files: bool):
    for root in _ROOTS:
        for path in sorted((ANSIBLE / root).rglob("*.yml")):
            parts = set(path.relative_to(ANSIBLE).parts)
            if (var_files and parts & _VAR_DIRS) or (not var_files and not parts & _NON_TASK_DIRS):
                yield path


def _tasks_in(doc) -> list[tuple[dict, object]]:
    if not isinstance(doc, list):
        return []
    if doc and isinstance(doc[0], dict) and "hosts" in doc[0]:
        return [pair for play in doc
                for key in ("pre_tasks", "tasks", "post_tasks", "handlers")
                for pair in _walk(play.get(key), play.get("no_log"))]
    return list(_walk(doc, None))


def _tree_tasks() -> list[tuple[str, dict, object]]:
    return [(str(path.relative_to(ANSIBLE)), task, no_log)
            for path in _files(var_files=False)
            for task, no_log in _tasks_in(yaml.safe_load(path.read_text(encoding="utf-8")))]


def _declared() -> set[str]:
    spec = importlib.util.spec_from_file_location(
        "onbox_config", ANSIBLE / "helpers" / "onbox_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return set(mod.secret_names())


def _secrets(tasks, definitions) -> set[str]:
    """The declared secrets, the registers of secret-file slurps, and every
    definition whose value references a secret, to a fixed point."""
    secrets = _declared()
    for _, task, _ in tasks:
        for key in _SLURPS & set(task):
            if task.get("register") and _SECRET_FILE.search(str(task[key])):
                secrets.add(task["register"])
    grown = True
    while grown:
        grown = False
        for name, value in definitions:
            if not _is_secret(name, secrets) and _references(_expressions(value), secrets):
                secrets.add(name)
                grown = True
    return secrets


def _tree_definitions() -> list[tuple[str, object]]:
    return [pair for path in _files(var_files=True)
            for pair in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).items()]


def _findings(tasks, secrets) -> dict[tuple[str, str], set[str]]:
    """(file, task name) -> the secrets it references, for each task that
    handles one outside no_log: true."""
    out = {}
    for where, task, no_log in tasks:
        refs = _task_references(task, secrets)
        if any(key in task and _SECRET_FILE.search(str(task[key])) for key in _SLURPS):
            refs.add("<slurped secret file>")
        if refs and no_log is not True:
            out[(where, str(task.get("name")))] = refs
    return out


def _environment_findings(tasks, secrets) -> dict[tuple[str, str], set[str]]:
    """(file, task name) -> the secrets a task's or block's environment
    references, under no_log or not."""
    out = {}
    for where, task, _ in tasks:
        refs = _references(_expressions(task.get("environment")), secrets)
        if refs:
            out[(where, str(task.get("name")))] = refs
    return out


def _tree_findings():
    tasks = _tree_tasks()
    return _findings(tasks, _secrets(tasks, _tree_definitions()))


def test_every_secret_bearing_task_sets_no_log():
    unexplained = {k: v for k, v in _tree_findings().items() if k not in ALLOWED}
    assert not unexplained, (
        "these tasks reference a secret outside no_log: true; set no_log: true on "
        "the task (this file's docstring says how a failure stays readable), or add "
        "the task to ALLOWED with the reason it prints none of it:\n  "
        + "\n  ".join(f"{f}: {n!r} references {sorted(r)}" for (f, n), r in sorted(unexplained.items())))


def test_no_secret_travels_through_an_environment():
    tasks = _tree_tasks()
    found = _environment_findings(tasks, _secrets(tasks, _tree_definitions()))
    assert not found, (
        "these environments carry a secret onto the module's command line, which "
        "no_log does not hide; pass it on stdin or through a helper that reads it on "
        "the host:\n  "
        + "\n  ".join(f"{f}: {n!r} passes {sorted(r)}" for (f, n), r in sorted(found.items())))


def test_every_allowed_task_still_references_a_secret():
    stale = set(ALLOWED) - set(_tree_findings())
    assert not stale, f"ALLOWED names tasks that no longer need an entry: {sorted(stale)}"


def _check(snippet: str, definitions=()) -> dict:
    tasks = [("x.yml", task, no_log) for task, no_log in _tasks_in(yaml.safe_load(snippet))]
    return _findings(tasks, _secrets(tasks, list(definitions)))


def test_the_check_catches_a_restic_env_without_no_log():
    assert _check("""
    - name: restic snapshots
      ansible.builtin.command:
        argv: [restic, snapshots]
      environment: "{{ backup_restic_env }}"
    """) == {("x.yml", "restic snapshots"): {"backup_restic_env"}}


def test_no_log_leaves_a_secret_environment_flagged():
    snippet = yaml.safe_load("""
    - name: restic snapshots
      ansible.builtin.command: restic snapshots
      environment: "{{ backup_restic_env }}"
      no_log: true
    - name: guarded
      no_log: true
      environment:
        AWS_SECRET_ACCESS_KEY: "{{ backup_s3_secret_key }}"
      block:
        - ansible.builtin.command: restic snapshots
    - name: a path
      ansible.builtin.command: helper
      environment:
        CATENA_KNOBS: /root/.catena-knobs.json
    """)
    tasks = [("x.yml", task, no_log) for task, no_log in _tasks_in(snippet)]
    assert _environment_findings(tasks, _secrets(tasks, [])) == {
        ("x.yml", "restic snapshots"): {"backup_restic_env"},
        ("x.yml", "guarded"): {"backup_s3_secret_key"},
    }


def test_no_log_on_the_task_its_block_or_its_play_clears_it():
    assert not _check("""
    - name: restic snapshots
      ansible.builtin.command: restic snapshots
      args:
        stdin: "{{ backup_s3_secret_key }}"
      no_log: true
    - name: guarded
      no_log: true
      block:
        - name: login
          ansible.builtin.command: kcadm.sh config credentials
          args:
            stdin: "{{ admin_password }}"
    """)
    assert not _check("""
    - hosts: all
      no_log: true
      tasks:
        - ansible.builtin.debug:
            var: cloudflare_api_token
    """)


def test_a_presence_test_and_an_asserts_that_carry_no_value():
    assert not _check("""
    - ansible.builtin.set_fact:
        _configured: "{{ (backup_s3_secret_key | default('')) | length > 0 }}"
    - ansible.builtin.assert:
        that:
          - admin_password is defined
          - admin_password == other
        fail_msg: the admin password is unusable
    """)


def test_a_value_built_from_a_secret_is_one():
    found = _check("""
    - name: ping
      ansible.builtin.uri:
        url: "{{ backup_healthcheck_url }}"
    """, definitions=[("backup_healthcheck_url", "{{ base }}/ping/{{ healthchecks_ping_key }}")])
    assert found == {("x.yml", "ping"): {"backup_healthcheck_url"}}


def test_a_slurped_store_and_its_result_are_secret():
    found = _check("""
    - name: read the store
      ansible.builtin.slurp:
        src: /etc/catena/config.json
      register: _store
    - name: take the token
      ansible.builtin.set_fact:
        _cf: "{{ (_store.content | b64decode | from_json).secrets }}"
    """)
    assert set(found) == {("x.yml", "read the store"), ("x.yml", "take the token")}


def test_the_check_reads_the_tree():
    tasks = _tree_tasks()
    secrets = _secrets(tasks, _tree_definitions())
    handling = [t for _, t, _ in tasks if _task_references(t, secrets)]
    assert len(tasks) > 800, f"only {len(tasks)} tasks found"
    assert len(handling) > 40, f"only {len(handling)} tasks reference a secret"
