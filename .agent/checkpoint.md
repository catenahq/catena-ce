# Checkpoint: ops issue 444

## Done

- Investigation: all nine roles read, constraints recorded in plan.md.
- Env: `uv sync` in `ansible/`; baseline of the touched tests passes (140 passed).
- ops cloned at /var/tmp/issues/444/ops (read-only, for the bench search).

## In flight

- Steps 1 to 5 built, uncommitted in the tree; manifests in /var/tmp/issues/444/notes/step{1..4}-*.manifest.md.
  Step 4 follow-up running: regen `_read_store.yml` sets `_cf_regen_zone`, not `cloudflare_zone`.

## Next

- Step 6: review done (fleet:code-reviewer, 4 findings, all fixed: verify refuses a second authorized_keys line;
  dispatch_path run/parse/assert shared in tasks/_run_helper.yml; a stale comment; catena-admin
  shell/settings/checks.go judged the moved cloudflared task under infrastructure, fixed in the catena-admin clone
  on the agent branch). Then commit per manifest (four catena-ce commits, one catena-admin commit).

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
