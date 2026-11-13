# Plan: ops issue 460, catena-ce CI "Duplicate sentences" on dev

## Cause

Commit 09dd7163 (fix(ansible): hold the container engine on every converge) wrote the sentence "A hold released by
hand comes back on the next converge" in both `ansible/reconcile/roles/host_maintenance/README.md:21-22` and the
comment above the hold task in `ansible/reconcile/roles/host_maintenance/tasks/main.yml:29-31`.
`.github/scripts/duplicate_sentences.py` fails on it (runs 43320910831 and 43321578932, reproduced locally).

## Steps

- [Done] Keep the sentence in the role README, which also names what releases the hold
  (`playbooks/uninstall.yml`); delete it from the task comment in `tasks/main.yml`.
- [Done] Gates: `python3 .github/scripts/duplicate_sentences.py`, prose, unicode and core-app-names gates,
  `ansible/tests/unit/test_docker_engine_pin.py`.

## Open questions

None.
