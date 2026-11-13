# Checkpoint: ops issue 460

## Done

- Read job, the failing log, the gate script and both files.
- Reproduced: `python3 .github/scripts/duplicate_sentences.py` in the clone failed with the same duplicate.
- Fix committed (da48c044): the sentence removed from the hold task's comment in
  `ansible/reconcile/roles/host_maintenance/tasks/main.yml`; the role README keeps it.
- Gates after the fix: duplicate sentences clean, prose clean, unicode clean, core app names clean.

## Hypotheses

1. Outage: ruled out; two consecutive dev runs (43320910831, 43321578932) fail the same step, and the gate is a
   local Python script with no network.
2. Duplicate prose from 09dd7163: confirmed; `git show --stat 09dd7163` touches both files and the gate names
   README.md:14 (paragraph start) and tasks/main.yml:29. The gate passes once the comment drops it.

## Facts

- README.md:20-22 holds the hold explanation plus the uninstall release.

## Commands that work

- cwd clone: `python3 .github/scripts/duplicate_sentences.py`
- cwd clone: `node ../contracts/scripts/check-prose.mjs`, `check-unicode.mjs`, `check-core-app-names.mjs .`

## Next

- Nothing: done.
