# Moving the reconciler into the image

State of the work that ends `catena prod converge` as routine maintenance.
Read this before touching `boundary.yml`, `playbooks/reconcile.yml`, or the
dispatch table, and update it when a phase moves.

The design write-up this implements:
<https://claude.ai/code/artifact/862fdb53-c9cb-4a8f-ada1-7d3cd1bdc841>

---

## The rule

Two questions, asked in order, decide who owns any given value.

1. **If this is wrong, is anything left that can fix it?** If no, it is
   BOOTSTRAP: an operator runs it, over SSH, from outside. Five roles, and the
   set is meant to stay small. The second admissible reason is "cannot be
   safely re-applied to a live host unattended", which is why `storage` is
   there.
2. **Otherwise: is the correct value a function of the product VERSION or of
   THIS HOST?** Version-shaped things travel in the image. Host-shaped things
   live in `/etc/catena/config.json`. Either way the role is RECONCILE and the
   host can apply it to itself.

The old line was "is this a file on the host", which is a storage location
rather than an owner. It put the Gatus endpoint list -- a list of things the
product decided to probe, containing nothing about the host -- on the operator's
path.

**The invariant that keeps the two apart:** a reconcile may not modify anything
that would remove your ability to run a reconcile.

---

## Where it stands

| Phase | State | Landed as |
| --- | --- | --- |
| 1a. Boundary declared + enforced | **done** | catena-ce `75a529c` |
| 1b. Directories physically split | **open**, bench-gated | -- |
| 2. Dispatch table into the image | **precondition done**, move open | catena-ce `db03b51` |
| 3. Inventory into the store | **open**, measured at 18 | gate in `75a529c` |
| 3'. Registry is the enforcement point | **done** | catena-ce `d63318a` |
| 4. On-host reconcile + panel button + timer | **vendoring done**, rest open | catena-admin `17c615f` |
| 5. Re-home split owners, retire the laptop path | open | -- |
| 6. Optional: Go reconciler | not started | -- |

Phases 3' and 4's vendoring landed early because both are independent of the
phases they are numbered under.

---

## What was discovered while building it

These change the plan. Each one is checked, not assumed.

### 1. Every action body is already constant-only

All 28 reserved action bodies (24 CE + 4 Business) interpolate 15 Ansible
variables between them, and **every one resolves to a fixed absolute path the
payload installer itself chose**: `/usr/local/bin/catena-recovery`,
`/var/lib/catena/selfupdate.state`, `/etc/catena/config.json`, and so on. They
are product constants written as inventory values.

So the dispatch table is converge-rendered by history, not by necessity, and
phase 2 has no blocker. Gated by
`tests/unit/test_action_bodies_are_constants.py`, which fails the build the
moment somebody proposes a body that would make the move impossible.

### 2. No precedence flip is needed, so the privilege question is moot

The plan flagged "let a drop-in override a base name" as a real privilege
decision needing deliberate sign-off, because the image would gain the ability
to redefine what root runs for an existing name.

It is not needed. When the 24 CE bodies move to the drop-in they are **removed**
from the converge's table, so the base table empties and the overlay becomes the
only source. The dispatcher's existing base-first precedence is untouched: names
it does not carry already fall through to `/etc/catena/admin-actions.d/`.

Do not add override semantics. If a future change seems to need them, that is
the signal that something was left in the base table which should have moved.

### 3. Community actions need dispatch arms, not declarations

`Manifest.Accepts()` has exactly **one** non-test caller:
`shell/convergestate/convergestate.go:139`. Phase 2 deletes convergestate, after
which nothing reads it.

`EntitledPayloadActions` filters declarations by licence entitlement and refuses
a panel-less one. That is exactly right for the CE actions: no licence-gated
panel owns them, and the pages that use them (Settings, Schedules, Restore)
dispatch them by name rather than rendering a button from host data.

So declare them **panel-less and hidden**. The model already expresses this: a
panel-less declaration is refused from being RENDERED
(`EntitledPayloadActions`) while still being COUNTED
(`PayloadActionNames`, `Accepts`), which is the state wanted. No change to the
authorization path, no sentinel "always allowed" panel.

### 4. The generator must declare constants once, at the top

The first generated drop-in inlined each constant at every call site. That
breaks properties the existing tests hold, e.g.
`test_recovery_binary_path_is_declared_once`.

Follow `catena-admin payload/actions.d/10-business.sh`: shell variables at the
top (`_CATENA_RECOVERY_BIN=/usr/local/bin/catena-recovery`), arms referencing
them. Generate, never transcribe -- 24 bodies retyped by hand is 24 chances to
change one.

### 5. The phase-3 backlog is 18 variables, not a vague pile

Measured by the gate in `tests/unit/test_converge_boundary.py`
(`INVENTORY_BACKLOG = 18`), which ratchets: the build fails if the number grows,
and it must be lowered by hand as they are removed.

```
admin_email                    catena-admin, coturn, infrastructure, keycloak
beszel_subdomain               infrastructure
catena_admin_subdomain         catena-admin
catena_admin_ui_port           catena-admin
cloudflare_zone                backup, catena-admin, cloudflare_tunnel,
                               cloudflare_tunnel_regenerate, coturn,
                               infrastructure, keycloak, oauth2_proxy
cloudflared_tunnel_name        catena-admin, cloudflare_tunnel,
                               cloudflare_tunnel_regenerate, infrastructure
coturn_acme_ca_bundle_b64      coturn
coturn_acme_directory_url      coturn
coturn_acme_host_ip            coturn
coturn_certbot_use_staging     coturn
healthchecks_subdomain         infrastructure
keycloak_realm_display_name    keycloak
keycloak_subdomain             infrastructure, keycloak
mailserver_acme_ca_bundle_b64  infrastructure
mailserver_acme_directory_url  infrastructure
mailserver_acme_host_ip        infrastructure
portainer_ui_port              catena-admin, infrastructure, portainer
storage_mount_point            backup, catena-admin, keycloak
```

Most are subdomains and the zone. The ACME ones (coturn, mailserver) are bench
and dev overrides rather than client-facing values, so they may become product
constants rather than store keys.

### 6. Three misclassifications the boundary gates caught

Writing the gates found these rather than confirming the guesses.

- A **restore** rewrites the host's SSH identity and is reached only from
  `playbooks/restore.yml`. It belongs to neither side; declared in
  `boundary.yml` as `operator_only_files`.
- `roles/backup` names `/etc/ssh` in its defaults because **restic reads it** --
  it is in the backup set with host keys excluded. The bootstrap-owned-path
  scan therefore covers task files only. A rule that cannot tell reading a path
  from writing it gets argued with rather than obeyed.
- `roles/common`'s **templates belong with its bootstrap half**, not its
  reconcile half. Hence `default_side` per straddling role.

---

## What remains, in order

### Phase 2: the dispatch table into the image

Cross-repo. Nothing else in this list is blocked by it.

**catena-admin**
1. Generate `payload/actions.d/20-catena.sh` from
   `catena_admin_ce_reserved_actions`, constants at the top (see finding 4),
   declarations panel-less + hidden (finding 3). A working generator is
   reproducible from findings 1 and 4; it must substitute the Jinja escapes
   `{{ '{{' }}` / `{{ '}}' }}` to literal braces first, because the
   `docker service inspect --format` bodies carry Go templates that only needed
   escaping while Ansible rendered them.
2. Delete `shell/convergestate/`, its banner block in
   `shell/web/templates/layout.tmpl`, the `converge.*` i18n keys in both
   translations, and the `Converge` field on `renderData`.
3. Extend `payload/actions.d/declared_panels_test.go` to cover the new file:
   it asserts panel presence for the Business drop-in, and the CE one asserts
   the opposite (panel-less by design, with the reason).

**catena-ce**
4. Empty `catena_admin_ce_reserved_actions`. Keep
   `catena_admin_ee_reserved_actions`' `ee-install-engines` **in the converge**:
   it is the action that reinstalls the payload, so it is the way back when a
   drop-in is broken. That is the reconcile invariant applied to the dispatch
   table itself.
5. `roles/catena-admin/tasks/catalog.yml` renders the table and the allow-list.
   The table becomes a loader-only template; decide whether
   `admin-allowed.yaml` (an audit artifact, not an enforcement point) is
   regenerated from `release.json`'s `payload_actions` or dropped.
6. Re-point 8 test files at the drop-in. **Roughly 40 assertions, each a real
   property.** Do not mechanise this: the assertions that check for
   `{{ catena_recovery_bin }}` become checks against the top-of-file constant,
   and that is a rewrite, not a substitution.

```
tests/unit/test_admin_dispatch_overlay.py            (14 tests)
tests/unit/test_catena_admin_backup_endpoint_actions.py
tests/unit/test_catena_admin_cloudflared_actions.py  (11 tests)
tests/unit/test_catena_admin_fss_action.py
tests/unit/test_catena_admin_migrate_lane.py
tests/unit/test_catena_admin_quiesce_actions.py      (14 tests)
tests/unit/test_catena_admin_self_update_actions.py  (9 tests)
tests/unit/test_release_manifest.py
```

**Ordering safety on a real host:** `roles/payload` installs the drop-ins at
role 5.5; `roles/catena-admin` renders the table at role 13. Within one converge
the drop-ins land first, so there is no window where the base table is empty and
the overlay is not yet installed.

### Phase 1b: move the directories

`bootstrap/` and `reconcile/` as sibling trees, `shared/` for filter plugins.
`roles/common` splits at `public_ports.yml`; `roles/catena-admin` splits at
`host.yml`. `boundary.yml` already declares every file's side, so this is a
rename rather than a judgement call.

**186 files reference `roles/<name>`**, 49 of them tests. The gate is a bench
install from zero producing the same host, so land it where a bench can run.

### Phase 3: the inventory into the store

Empty the 18. Each becomes a registry-declared store key or a compiled-in
product constant, and nothing else. Lower `INVENTORY_BACKLOG` as they go.

Done when `playbooks/reconcile.yml` runs to completion against an empty
inventory: no `.env`, no group_vars, no `-e`.

### Phase 4: run it on the host

- Bootstrap installs a pinned `ansible-core` venv at `/opt/catena/ansible`. The
  playbook declares a minimum version and the reconcile refuses loudly rather
  than failing midway.
- Engine verb extracting the tree from `/usr/local/share/catena-ce` into a
  root-owned 0700 stage dir, digest-checked, the way
  `stackupdate.installPayloadMain` already does it.
- Actions `catena-converge` and `catena-converge-status`, detached under
  `systemd-run --unit catena-converge --collect`, behind
  `flock /run/catena.lock`.
- Panel section under Settings reusing the `panelupdate_status` fragment shape.
  The state file, the log tail through the status action, and the htmx poll that
  survives the panel restarting all already exist (catena-admin `7dfc28a`).
- Timer from the start. **Gate first:** a reconcile against an unchanged store
  must report zero changed tasks and restart nothing, proven on the bench,
  or the timer is a scheduled outage.

### Phase 5 and 6

Re-home the split unit owners (`catena-dashboard-sync.service` ships in the
image while its `.timer` is converge-rendered), unify the settings schema, and
retire `catena converge` to bootstrap and DR. Then optionally replace Ansible
with per-concern Go engines, following
`catena-cloudflared-sync` / `catena-dashboard-sync` / `catena-schedule`.

---

## Traps

- **`vendor/` is unusable as a directory name in catena-admin.** Go reads a
  top-level `vendor/` as module vendoring and a `vendor/` with no
  `modules.txt` fails every `go build` in the repo. The staging directory is
  `third_party/catena-ce`.
- **`docker buildx imagetools` contains `docker build` as a substring.** Any
  guard matching build invocations needs a word-boundary regex, or the digest
  resolution and the `:latest` retag register as builds.
- **The vendored tree takes its file list from `git ls-files`**, so catena-ce's
  own `.gitignore` decides what a public image publishes. `inventory/*` is
  ignored with `example/` and `README.md` carved back out, which is exactly the
  wanted result. Do not replace this with an exclude list here: a second copy of
  that rule can drift, and the copy that drifts is the one publishing client
  data. A real inventory is per-client information; treat that folder carefully.
- **`swarm_service_drift` compares image refs asymmetrically** and this is
  unfixed. `_strip_digest` is applied to the live side only while
  `catena_admin_image` carries its digest, so the filter reports drift against
  its own pin and emits `--image` on every converge. Docker no-ops the identical
  update so nothing restarts, but the task reports `changed` every time and
  "did the converge move my panel" is unanswerable from the output. The unit
  tests pass because they use a digest-less desired image.
- **A converge re-pins the panel image.** `catena_image_pin` takes
  `max(newest published, on-host pin)`, so a deliberate DOWNGRADE chosen in the
  panel does not survive a converge. Moving forward does.

---

## Free hand

No clients are running this. Nothing here needs a migration path, a
compatibility shim, or a store format an older build can read. Validation is a
bench install from zero, not an upgrade in place. That is what makes the
dispatch table move wholesale instead of being shadowed, and the inventory move
in one push instead of role by role.
