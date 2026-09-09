# roles/ansible_runtime

The ansible-core the host reconciles itself with, installed into its own venv at
`/opt/catena/ansible`.

## Why it is bootstrap-side

The boundary's first question: if this is wrong, is anything left that can fix
it? No. A reconcile is what this makes possible, so a reconcile cannot install
it. Same reason `roles/docker` sits on that side.

## Why it is not the operator's ansible

The operator's controller environment comes from `ansible/pyproject.toml`
through `uv`, on their laptop. This one is on the client VPS. They pin the same
range deliberately -- `tests/unit/test_ansible_runtime_pin.py` fails when they
drift -- because the bench validates one ansible-core line, not two.

## Why it is not on PATH

Nothing dispatches these binaries by name. The converge engine
(catena-admin `payload/cmd/catena-converge`) names the interpreter it wants, so
putting `ansible-playbook` in `/usr/local/bin` would only create a second way to
run a converge that nobody audited.

## The version is checked twice

Here, after installing, because a venv whose `pip` succeeded and whose
`ansible-playbook` cannot start is a failure this role can see. And again in
`playbooks/reconcile.yml`, because the host that runs a reconcile a year from
now is running whatever it was given then -- `ansible_runtime_spec` is what to
install, `ansible_runtime_minimum` is what may run.
