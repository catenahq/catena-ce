# Plan: ops issue 444, every role has a validate.yml

Nine roles have no `tasks/validate.yml`: bootstrap `ansible_runtime`, `catena_admin_host`; reconcile
`catena-admin`, `cloudflare_tunnel`, `cloudflare_tunnel_regenerate`, `dispatch_path`, `payload`, `public_ports`,
`tier1_stack`. Each gets one, run by `ansible/playbooks/validate.yml` Vantage 1 through `validate_roles`
(`include_role: tasks_from: validate`), with a negative check for what the role closes.

## Constraints found (file:line)

- Reconcile task files may not name a `bootstrap_owned_paths` literal (`/etc/ssh/`, `/etc/sudoers.d/`,
  `authorized_keys`, `/usr/local/lib/catena-dispatch/`, ...), even to read it:
  `ansible/tests/unit/test_converge_boundary.py:192`. Bootstrap task files may not name a `reconcile_owned_paths`
  literal (`daily.env`, `catena-admin.json`, ...): same file :337.
- No reconcile file may name `catena_migrate_user` or `31-catena-migrate`:
  `ansible/tests/unit/test_catena_admin_migrate_lane.py:236`.
- Reconcile files name no inventory (`dotenv`) variable: `test_converge_boundary.py:399`.
- The payload decision is made only by `bootstrap/roles/common/tasks/_payload_expected.yml`; every caller is listed
  in `ansible/tests/unit/test_payload_expected_predicate.py:29` CALLERS.
- `validate_roles` stays a literal: the ops audit parser reads it (`ops/automation/audit/parsers/ansible.py:217`).
- Ansible's stat gives no `pw_name` for an unnamed uid; the panel's files belong to uid 65532
  (`playbooks/group_vars/all/main.yml:439`).
- The cloudflared 1/1 check and the edge verdict `_infra_edge_up` live in
  `reconcile/roles/infrastructure/tasks/validate.yml:6-34,156-187`; Vantage 2 (`playbooks/validate.yml:337`) and
  `reconcile/roles/oauth2_proxy/tasks/validate.yml:41` read the verdict.

## Steps

1. [Done] dispatch_path. `helpers/dispatch_path.py` gains `--verify`: writes nothing; ok only when the panel key's
   line runs this tree's generation, its directory is intact, the link, the sudo and AcceptEnv rules are this
   generation's, no `-next` rule, other generation or proof key directory is left (negative), and the panel key's
   proof passes (the entry refuses an extra word: negative). The input dict moves to the role defaults so
   `tasks/main.yml` and `tasks/validate.yml` send the same one. `tasks/validate.yml` runs the tree's helper with
   `--verify` and asserts. Tests: `tests/unit/test_dispatch_path.py` (verify ok after a switch and writes nothing;
   fails on each left-over or mismatch; validate.yml runs `--verify`).
2. [Done] Bootstrap roles.
   - `ansible_runtime`: the version check moves from `tasks/main.yml` into `tasks/validate.yml`, which main.yml
     includes; validate adds the venv is root-owned 0755 (negative: nothing but root changes what the reconcile
     runs).
   - `catena_admin_host`: runner and migration accounts (shell, home, shadow password `*` not locked), runner
     `~/.ssh` 0700 runner-owned plain directory, `/etc/catena`, the panel key dir and files (uid 65532, 0700/0600,
     not links), known_hosts non-empty with every line keyed `host.docker.internal`, the ssh config strict,
     stats/state/export dirs, and `sshd -T -C user=<migrate>` resolving the Match block (negative: no tty, no
     forwarding, key file and forced command fixed). `common/tasks/_assert_path_perms.yml` gains an optional
     `uid` key.
3. [Done] Reconcile roles without an edge.
   - `payload`: with `catena_payload_install`, the marker equals the image ID of `catena_admin_image`; the stage
     dir, when present, root 0700 (negative: the panel cannot write what root installs); the catalog cache absent.
   - `public_ports`: fragment dir root 0755 (negative: only root declares a port), docker drop-in root 0644
     carrying the start line; with the reconciler expected (shared predicate, CALLERS entry), the unit present and
     its timer enabled and active.
   - `tier1_stack`: stack dir root 0750, `tier1.yml` root 0640 (negative), `docker stack config` loads it and it
     holds every `tier1_stack_required` service.
   - `catena-admin`: lane files and modes (`stack-update.env` 0600: negative), the UI-port fragment declares
     `catena_admin_ui_port` with scope `loopback` (negative), catalog files root 0644, overlay dir root 0755
     (negative), and with `catena_admin_deploy_from_converge` the service at 1/1.
4. [Done] The edge.
   - `cloudflare_tunnel`: owns the edge verdict and the cloudflared check, moved out of infrastructure's validate;
     the verdict is renamed `_cf_edge_up` and read by Vantage 2 and oauth2_proxy's validate; the engine path from
     the role's own default; adds cloudflared publishes no port (negative: the tunnel is outbound only). Tests:
     `test_oauth2_proxy_validate_follows_the_tunnel.py`, `test_payload_expected_predicate.py` CALLERS.
   - `cloudflare_tunnel_regenerate`: the store read and the tunnel lookup move into task files main.yml and
     validate.yml share; validate, with a token and a zone in the store, asserts at most one live tunnel carries
     this host's name (negative: a rotation leaves no old tunnel), and exactly one when `_cf_edge_up`.
5. [Done] `playbooks/validate.yml` `validate_roles` lists all 22 roles; a new unit test
   `tests/unit/test_every_role_has_a_validate.py`: every role directory under both roots has `tasks/validate.yml`
   and is in `validate_roles`.
6. [Done] Review (fleet:code-reviewer), gates (Ansible unit tests, prose, unicode, core app names), commit.
7. [Done] catena-admin `shell/settings/checks.go` `ChecksJudging`: the Domain section is judged by
   `cloudflare_tunnel`'s cloudflared tasks and by `cloudflare_tunnel_regenerate`, since the cloudflared check left
   infrastructure's validate (`shell/settings/checks_judging_test.go` reads the sibling catena-ce).

## Bench to prove it

`ce_install_suite` (the install's validate: no domain, tunnel deferred) and `ce_validate` (domain, tailnet and
backup entered: the tunnel, the regenerate lookup and the dispatch proof all run).

## Out of this repo

- ops `internal_docs/generated/` (feature grid edges for nine new validate files): the clerk regenerates.
- ops `internal_docs/tools/validate.md` if it lists the validated roles.
