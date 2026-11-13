# Checkpoint: ops issue 444

## Done

- Investigation: all nine roles read, constraints recorded in plan.md.
- Env: `uv sync` in `ansible/`; baseline of the touched tests passes (140 passed).
- ops cloned at /var/tmp/issues/444/ops (read-only, for the bench search).

## In flight

- Step 1 (dispatch_path).

## Next

- Steps 2 to 6, one implementer at a time; briefs in /var/tmp/issues/444/briefs, notes in /var/tmp/issues/444/notes.

## Hypotheses

- Not a defect hunt: a feature. No hypothesis ruled out.

## Commands that work

- Tests (cwd /var/tmp/issues/444/catena-ce/ansible):
  `flock /tmp/catena-gates.lock uv run pytest -q -p no:cacheprovider tests/unit/test_x.py`
- Gates (cwd /var/tmp/issues/444/catena-ce): `node ../contracts/scripts/check-prose.mjs`,
  `node ../contracts/scripts/check-unicode.mjs`, `node ../contracts/scripts/check-core-app-names.mjs .`

## Facts

- The bench runs `validate.yml --tags vantage1 --skip-tags smtp` after every on-host converge
  (ops `automation/tests/README.md:27`); no bench file names an infrastructure validate task.
