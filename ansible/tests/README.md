# Tests

`unit/` holds plain pytest over the installer, the helpers, the filter plugins,
the shipped scripts and the Ansible tree. It needs Python and the tree's own
ansible-core, and runs with `uv run pytest` from `../` (pytest's `testpaths`)
and in the repo's CI on every change.

Most tests read the Ansible tree as data (asserting that a role's tasks carry
`no_log`, that a timer's cadence stays within the weekly cap, that a template
renders a specific field): that is how a structural invariant gets a test
without a converge. A test that needs a task's runtime behaviour runs those
tasks alone under `ansible-playbook` on localhost, against a stand-in server on
127.0.0.1, as `unit/test_tunnel_delete_outlasts_a_reconnect.py` does.

## End-to-end coverage

Install, break, restore and upgrade on disposable VMs run in the maintainers'
rehearsal suite, outside this repository. Every scenario it carries, and the
behaviour each one proves, is listed in
[../../TEST_SCENARIOS.md](../../TEST_SCENARIOS.md).
