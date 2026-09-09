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
| 2. Dispatch table into the image | **done** | catena-admin `6557adf`, catena-ce `829b914` |
| 3. Inventory into the store | **open**, measured at 18 | gate in `75a529c` |
| 3'. Registry is the enforcement point | **done** | catena-ce `d63318a` |
| 4. On-host reconcile + panel button + timer | **vendoring done**, rest open | catena-admin `17c615f` |
| 5. Re-home split owners, retire the laptop path | open | -- |
| 6. Optional: Go reconciler | not started | -- |

Phases 3' and 4's vendoring landed early because both are independent of the
phases they are numbered under.

The two defects this document used to list as unfixed are fixed: catena-ce
`317e3da` (the drift comparison and the image pin) with catena-admin `3a1b54c`
(the rollback's half of the second one).

---

## What was discovered while building it

These change the plan. Each one is checked, not assumed.

### 1. Every action body is already constant-only

All 28 reserved action bodies (24 CE + 4 Business) interpolate 15 Ansible
variables between them, and **every one resolves to a fixed absolute path the
payload installer itself chose**: `/usr/local/bin/catena-recovery`,
`/var/lib/catena/selfupdate.state`, `/etc/catena/config.json`, and so on. They
are product constants written as inventory values.

So the dispatch table was converge-rendered by history, not by necessity, and
phase 2 had no blocker. The gate that proved it,
`tests/unit/test_action_bodies_are_constants.py`, is gone with the move: the
bodies it guarded are in the image, and what replaces it is the other half of
the same question -- `tests/unit/test_converge_dispatch_table.py` asks why each
action that STAYED cannot be in the image, and catena-admin
`payload/actions.d/catena_actions_test.go` asserts the constants where they now
live.

### 2. No precedence flip is needed, so the privilege question is moot

The plan flagged "let a drop-in override a base name" as a real privilege
decision needing deliberate sign-off, because the image would gain the ability
to redefine what root runs for an existing name.

It was not needed. The 24 CE bodies were **removed** from the converge's table
as they moved, so the base table has nothing to shadow and the overlay is simply
the only source. The dispatcher's base-first precedence is untouched: names it
does not carry already fall through to `/etc/catena/admin-actions.d/`.

Do not add override semantics. If a future change seems to need them, that is
the signal that something was left in the base table which should have moved --
and `test_converge_dispatch_table.py` will name it.

### 3. Community actions need dispatch arms, not declarations

`Manifest.Accepts()` had exactly **one** non-test caller,
`shell/convergestate/convergestate.go:139`, and phase 2 deleted convergestate
with its banner, its translations and its plumbing. The banner asked whether the
host's converge predated the panel's actions; every action now either ships in
the image beside the panel or is a converge action the panel does not dispatch,
so the question has no answer left to give.

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
`test_recovery_binary_path_is_declared_once` -- and it is also what makes the
missing-binary tests possible at all, since they work by repointing the `*_BIN`
variables.

`catena-admin payload/actions.d/catena_actions_test.go` now enforces it for
every drop-in: no `/usr/local/bin/`, `/var/lib/catena/` or `/etc/catena/` path
may appear inside an arm.

Generate, never transcribe -- 24 bodies retyped by hand is 24 chances to change
one. The generator also has to substitute the Jinja escapes `{{ '{{' }}` /
`{{ '}}' }}` to literal braces first, because the `docker service inspect
--format` bodies carry Go templates that only needed escaping while Ansible
rendered them. It carried each action's comment block across verbatim: the
comments are the reasoning, and rewriting 24 of them from memory loses it.

### 5. The phase-3 backlog was 18 variables, and 13 of them were constants

Measured by the gate in `tests/unit/test_converge_boundary.py`
(`INVENTORY_BACKLOG`), which ratchets: the build fails if the number grows, and
it must be lowered by hand as they are removed. It was 18; it is 10.

**What the evidence said when the eighteen were actually read.** Comparing every
one against the shipped starter inventory, the operator skeleton in ops, and the
one real inventory:

- **Exactly one is a hard blocker.** `cloudflare_zone` has no default, so
  `lookup('dotenv')` raises and the play stops. Everything else has a default
  and the reconcile runs without an inventory today.
- **Thirteen were product constants written as inventory values.** Every source
  set them to the same string, or did not set them at all: the public
  subdomains, the two UI ports, the storage mount point, the realm display name,
  and the ACME knobs. Removing the lookup changed nothing anywhere, which is
  what says they were never inventory values.
- **One disagreed, and that is what made the rest decidable.**
  `PORTAINER_SUBDOMAIN` is `apps` in the operator skeleton, `portainer` in the
  starter, and `admin` for established clients per its own comment. Three
  sources, three answers: a host fact. It stays a lookup until it is a store
  key.

**The dangerous class is not the blocker.** A missing value with no default
stops the play, loudly, once. A missing value WITH a default reconfigures the
host quietly and reports success -- so the phase-3 acceptance test as written,
"reconcile.yml runs to completion against an EMPTY inventory", would pass while
producing a wrong host. The gate has to be "runs to completion AND produces the
same host", which is a bench assertion rather than a unit one.

**The count is a floor.** `_backlog()` matches variable names in reconcile
files, so a value the inventory supplies and a role reads through a derived name
-- `infrastructure_gatus_hostname`, say -- is not counted. Those all bottom out
in `cloudflare_zone`, which is counted, so zero still means what it says.

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

The eleven that left are gone from `BOOTSTRAP_CONFIG`, from the starter
inventory and from the operator skeleton, because a knob that no longer turns
anything is worse in the documentation than absent.

What remains, and what each needs:

| Variable | Needs |
| --- | --- |
| `cloudflare_zone` | a store key. The hard blocker, and eight roles read it |
| `admin_email` | a store key |
| `portainer_admin_subdomain` | a store key; the one subdomain that varies |
| `cloudflared_tunnel_name` | the prefix is a bench input; the name is derivable on-box |
| `coturn_acme_*` (4), `mailserver_acme_*` (3) | product constants with a bench escape hatch |

The ACME seven are the awkward ones: they are bench overrides seeded into the
inventory `.env` by ops `install_yaml.py`, so making them constants needs the
bench to pass them another way (an exported env var, the shape
`CATENA_ADMIN_IMAGE` already uses). That is a cross-repo change gated on a bench
run, which is why they were not done with the other thirteen.

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

### 7. The image has to SHIP every command it authorises

The one thing that made phase 2 look impossible, and the rule that resolved it.

Three of the twenty-four bodies ran one of two catena-ce scripts the converge
installed by hand: `onbox_config.py`, the settings store writer that serves the
whole Settings tab, and `catena-restic-key.py`. An image declaring an action for
a path something else installs is an action that works or not depending on how
the host was converged --
`TestEveryDispatchedBinaryIsOneThePayloadInstalls` in catena-admin says so, and
it is right.

Resolved by delivery, not by exception: the image already carries the whole
vendored catena-ce tree, so the build copies those two into the payload's own
`bin/` and `roles/payload` installs them at role 5.5 with the engines. One
source file, still in catena-ce; who ships it changed, which is the answer the
engines already had. `roles/catena-admin/tasks/host.yml` no longer installs
either.

Note what this does NOT weaken. Once the drop-in is image-owned, "which binary
an arm names" is already the image's choice, so shipping the writer changes no
boundary. The boundary that holds is elsewhere and stays converge-owned: the env
allow-list, and the registries in `helpers/onbox_config.py`, which the converge
reads AGAIN on the way out -- a key this repo does not declare is a key it never
projects, whatever the file holds.

### 8. Two image comparisons answered for the wrong host

Both were found while moving the table and are now fixed (`317e3da`,
catena-admin `3a1b54c`). They are recorded because the shape recurs.

`swarm_service_drift` stripped the digest from the LIVE side only, so a
digest-carrying pin never matched itself. The fix is NOT to strip both sides --
that answers "converged" for a host running different bytes under the same tag.
What the comparison does depends on what the PIN says.

`catena_image_pin` conflated "what to run with no pin" with "the oldest version
a pin may name". One literal answers both wherever the converge ships a version;
the panel's first argument is the newest published release, so max() made the
pin inert and a rollback chosen in the panel was undone by the next converge.
The two jobs are separate arguments now, and the panel's rollback records the
version it went back to instead of clearing the pin.

---

## What remains, in order

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
- **The CE reserved list was DELETED, not emptied.** The dispatcher is
  base-first, so a name in the converge's table wins outright and the drop-in
  where that command is maintained is never reached -- silently, with both files
  passing every assertion about themselves. An empty list left behind is a place
  for one to come back to. `tests/unit/test_converge_dispatch_table.py` is the
  gate: the reserved table must equal a declared set, each entry carrying the
  reason the image cannot hold it. Three names are left, and two of them are
  the cloudflared pair, which moves as soon as phase 3 puts
  `cloudflared_tunnel_name` in the store.
- **A dead action hid behind a plausible reason.** `cloudflare-zones-save` sat
  in the converge's table because its command was not a payload binary -- and
  the reason it was not one is that NO repository built it, so the Domains
  panel's save could only ever answer "command not found". The declared-set gate
  is what surfaced it: made to write down why the image could not carry the
  action, the honest answer turned out to be that nothing carried it. Fixed by
  writing the engine (catena-admin `payload/cmd/catena-cloudflare-zones`), which
  was a wrapper over a model the panel already carried and tested.
- **The payload installer's prune leg deletes by CONTENT.** Two of its binaries
  are catena-ce scripts now, and the converge no longer writes them. Withdrawing
  one from the image therefore removes it from the host, which is correct and
  worth knowing before moving a file in catena-ce.

---

## Free hand

No clients are running this. Nothing here needs a migration path, a
compatibility shim, or a store format an older build can read. Validation is a
bench install from zero, not an upgrade in place. That is what makes the
dispatch table move wholesale instead of being shadowed, and the inventory move
in one push instead of role by role.
