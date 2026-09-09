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
| 3. Inventory into the store | **done**, 18 -> 0 | catena-ce `836261a` `c29b978` `4893394` `c677205` `95ed945` |
| 3'. Registry is the enforcement point | **done** | catena-ce `d63318a` |
| 4. On-host reconcile + panel button + timer | **built**, timer ships DISABLED | catena-ce `6a0473b`; catena-admin `a9dbd54` `81a6d15` `96c230d` |
| 5. Re-home split owners, retire the laptop path | open | -- |
| 6. Optional: Go reconciler | not started | -- |

Phases 3' and 4's vendoring landed early because both are independent of the
phases they are numbered under.

Also landed alongside: the engine the Domains panel had been dispatching at
nothing for as long as the panel has existed (catena-admin `778f54b`, catena-ce
`2a63bf2`), and the two image-comparison defects this document used to list as
unfixed -- catena-ce `317e3da` with catena-admin `3a1b54c` for the rollback's
half of the second one.

**Everything still open is bench-gated**, and everything that could be settled
from a laptop has been. Phase 1b's mechanical half is proven by
`test_every_role_reference_resolves.py` (`7669bc9`); what is left of it is
whether the split produces the same host. Phase 3 reaches zero, but zero is a
floor and the real acceptance test is a bench install that produces the same
host rather than one that merely completes. Phase 4's timer is explicitly gated
on a proven no-op reconcile.

So the next thing this work needs is a bench run, not another unit test.

---

## Decisions taken

### The store is born at BOOTSTRAP (2026-09-09) -- BUILT, catena-ce `d4b74f4`

**Decided: bootstrap seeds the config half of the store before any role runs.**

The question came from a defect the phase-3 gate surfaced. `bootstrap/roles/tailscale`
resolves its provider, control URL and Headscale user from `cfg_*` facts with no
inventory fallback, and `playbooks/bootstrap.yml` runs that role -- with no
loader, because the machine has nothing on it yet. So every one of them is empty
at bootstrap and the provider inference answers `tailscale` unconditionally. A
Headscale install then takes Fork A of the role, which exchanges Tailscale OAuth
credentials the operator does not have, and bootstrap fails at the exchange.
Confusing rather than dangerous, and only truly wrong for an operator who runs
both tailnets and supplied both credentials.

Four ways out were considered. The two rejected outright:

- **An inventory fallback for the tailnet variables.** Smallest diff, and it
  reintroduces two live readers of one value -- the exact defect the store move
  exists to end. It also fixes one variable and nothing else: the next key moved
  into the store hits the same wall.
- **Accept bootstrap-then-correct.** Zero work, and the end state on the happy
  path is already right. But it leaves a Headscale operator meeting an error
  that names Tailscale on a host they never meant to put there.

The one deferred:

- **Thread the values into bootstrap as `-e`,** the channel the CLI already uses
  for the install-critical vendor credential (`ansible/seed.py`: the Tailscale
  OAuth pair is "the ONLY install-critical vendor cred"). Small, consistent, no
  new store semantics. Rejected as the ANSWER because it is per-install rather
  than persistent -- it says nothing to a later `rotate-tailscale` on a host
  whose store was lost -- and because it makes the `-e` file a second seed
  surface beside `.env`, which is one more place a value can come from.

**Chosen: bootstrap writes the store first.** It is the only option that scales
with the migration rather than routing around it: every subsequent phase-3 key
inherits a store that exists from the first minute, and the exception disappears
instead of being managed. It makes "the store is the source of truth" true from
minute one rather than from the first converge.

What it entailed, and the part that needed care. The seed is in Phase 2 of
`bootstrap.yml`, the play that runs `bootstrap/roles/tailscale`, as a pre_task -- Phase 1
can end early on its own idempotency check, Phase 2 always runs, and by then the
common role has put python3 and the `ops` account on the box.

1. Extract the CONFIG block of `playbooks/tasks/load_onbox_config.yml` -- the
   four tasks from "read the store-owned config key names" through "set_fact
   each store-owned config value", plus the zone assert -- into its own include,
   e.g. `tasks/seed_onbox_config.yml`. The full loader keeps including it, so
   there is one copy.
2. Include that, and only that, from `playbooks/bootstrap.yml` as a pre_task.
3. **Do NOT run the rest of the loader at bootstrap.** The secrets block MINTS
   the internal secrets and the user-held admin/restic DR keyset, and the
   release block makes a network call to the container registry before Docker is
   even installed. Both belong to a converge. Pulling in the whole loader
   because it is one file would be the mistake here.
4. `onbox_config.dump()` already does `p.parent.mkdir(parents=True)`, so the
   store lands on a bare machine with no `/etc/catena` -- checked, not assumed.
5. Delete `STORE_PREDATES_THE_PLAY` from
   `tests/unit/test_store_readers_load_the_store.py`. That table exists only to
   hold this exception, and a gate carrying an exemption for a thing that has
   been fixed is a gate that has started lying. Its sibling assertion
   (`test_the_play_that_predates_the_store_is_the_only_one`) goes with it.

Step 3 is held by a gate rather than by memory:
`test_bootstrap_seeds_the_store_and_runs_nothing_else` asserts bootstrap
includes the seed and does NOT include the loader, and
`test_the_seed_is_the_loaders_own_config_block` asserts the seed has not grown a
mint, a release resolution or a pin read.

**Gate: a bench install from zero.** This is an install-path change, so no unit
test settles it. Two things to watch on that run: the store exists and is
populated before `bootstrap/roles/tailscale` executes, and a Headscale inventory reaches
the Headscale fork rather than the OAuth one.

Recorded in `ops/BACKLOG_TECHNICAL.md` with the failure analysis.

---

### Phase 3 goes to ZERO rather than stopping at "declared" (2026-09-09)

The ACME four could have stopped where they were. `CATENA_ACME_DIRECTORY_URL`,
`CATENA_ACME_HOST_IP`, `CATENA_ACME_CA_BUNDLE_PEM_B64` and
`COTURN_CERTBOT_STAGING` were in `BOOTSTRAP_CONFIG` with a comment saying they
were bench overrides decided before the host exists, which is a declared,
defensible resting place.

**Decided: finish the move.** The test is not who sets a value, it is which side
of the boundary READS it -- and `reconcile/roles/coturn` and `reconcile/roles/infrastructure` are
reconcile-side. A host that converges itself cannot ask an operator's laptop
which certificate authority to trust. That a test harness is the only thing that
ever sets them does not move the reader.

The tell that this was the right reading: `MAILSERVER_CERTBOT_STAGING` was
already a settings key, and its coturn twin had been left in the `.env` with no
reason written down anywhere. One of a pair in each place is what an enumerated
backlog is for.

Rejected: `lookup('env', ...)`, the shape `CATENA_ADMIN_IMAGE` uses. It works,
and it is a cross-repo change (ops has to export before catena-ce stops reading
the `.env`, or the bench's Pebble path breaks) buying a worse answer -- a value
that lives for one process rather than on the host that needs it.

**Known consequence, not papered over:** the store seeds fill-only, so a bench VM
that outlives a change of the Pebble host's bridge IP keeps the old
`CATENA_ACME_HOST_IP`. Fresh installs and rewound VMs re-seed. A re-converge of a
long-lived VM after the bench host moved needs the key re-set or `/etc/catena`
removed.

### A bench-only knob may be a store key, if it says so (2026-09-09)

`CLOUDFLARED_TUNNEL_NAME_PREFIX` tags the tunnels the bench creates so its
cleanup can tell them from real ones in a Cloudflare account holding both. It is
empty on every client install and it is now in every client's store.

**Decided: that is acceptable, and each such key carries a comment beside it
saying it is a test-bench knob and why it is a store key rather than a `.env`
one.** Both the prefix and the ACME four have one.

The prefix earns it: putting it back in the `.env` puts the tunnel NAME back in
the converge, and the name is the only thing that kept `cloudflared-check` and
`cloudflared-sync` in the converge-rendered dispatch table. The engine composes
`<prefix><the box's hostname>` for itself now, both actions ship in the image,
and the reserved table is down to one entry.

`bootstrap/roles/common` sets the box's hostname to `inventory_hostname`, so every
converged host keeps the name it had. This is worth knowing before anybody
"fixes" the two back into agreement: they already agree.

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

`cloudflare_zone` and `admin_email` moved next, into `SETTINGS_CONFIG` where the
existing seed-once path picks them up with no new machinery: the `.env` fills
them on the first converge and the store answers ever after. The zone keeps NO
default, because an empty one builds `dash.`, `auth.`, `monitor.` and converges
a host that looks finished and serves nothing -- the loader asserts it resolved,
so the failure is one sentence at the start instead of a certificate for a blank
domain three roles later.

What remains, at 8:

| Variable | Needs |
| --- | --- |
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
- `reconcile/roles/backup` names `/etc/ssh` in its defaults because **restic reads it** --
  it is in the backup set with host keys excluded. The bootstrap-owned-path
  scan therefore covers task files only. A rule that cannot tell reading a path
  from writing it gets argued with rather than obeyed.
- `bootstrap/roles/common`'s **templates belong with its bootstrap half**, not its
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
`bin/` and `reconcile/roles/payload` installs them at role 5.5 with the engines. One
source file, still in catena-ce; who ships it changed, which is the answer the
engines already had. `reconcile/roles/catena-admin/tasks/host.yml` no longer installs
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

### 9. Moving a key changes which PLAYS can resolve it

The hop from `.env` to store is invisible at the call site -- roles say
`cloudflare_zone`, not `cfg_cloudflare_zone` -- so moving a key silently changes
which plays can still see it. The value now comes from a task that only some
plays run.

Moving the zone broke exactly one play, and not loudly:
`regenerate-cf-tunnel.yml` has no loader by design (it is dispatched from the
panel with the token on the command line), read the zone from the inventory, and
would have started regenerating a tunnel against an empty domain. It already
slurps the store for the token it rotates, so it now takes the zone from the
same read.

`tests/unit/test_store_readers_load_the_store.py` is the general form: a play
whose roles read a store-backed variable loads the store, or is declared with
the reason it does not need to. Writing it turned up a defect that predates all
of this -- `bootstrap/roles/tailscale` resolves its provider, control URL and Headscale
user from `cfg_*` with no inventory fallback, and two plays ran it without the
loader. `rotate-tailscale.yml` is fixed (it runs against an installed host, so
the loader is simply correct there). `bootstrap.yml` cannot be: it runs before
there is a store, which means a Headscale host bootstraps against Tailscale SaaS
and only the first `site.yml` puts it right. Declared in that gate, recorded in
`ops/BACKLOG_TECHNICAL.md`, and the fix is a decision about where the store is
born rather than a line in a playbook.

## What remains, in order

### Phase 1b: move the directories

`bootstrap/` and `reconcile/` as sibling trees, `shared/` for filter plugins.
`bootstrap/roles/common` splits at `public_ports.yml`; `reconcile/roles/catena-admin` splits at
`host.yml`. `boundary.yml` already declares every file's side, so this is a
rename rather than a judgement call.

**186 files reference `roles/<name>`**, 49 of them tests. The gate is a bench
install from zero producing the same host, so land it where a bench can run.

### Phase 3: the inventory into the store -- DONE

Zero. Every value a reconcile role reads is a store key or a compiled-in product
constant. `INVENTORY_BACKLOG = 0` and the ratchet holds against a new one.

**What zero does and does not claim.** It claims no reconcile role names an
inventory-defined variable directly, which is checkable from outside. It does
NOT claim the host produces the same result with no inventory at all -- the
count is a floor, because a value reached through a derived name is not counted,
and because seventeen of the eighteen had defaults, so a missing one
reconfigures quietly and reports success. **The remaining proof is a bench
install that produces the same host, not one that merely completes.**

### Phase 4: run it on the host -- BUILT, except the timer

Everything is in place for a host to converge itself, and one piece is
deliberately switched off.

- **The runtime.** Bootstrap installs a pinned ansible-core venv at
  `/opt/catena/ansible` (`bootstrap/roles/ansible_runtime`, catena-ce `6a0473b`). Not on
  PATH: the engine names the interpreter it wants, so a second unaudited way to
  run a converge never exists. The version is checked twice, because "what to
  install" and "what may run" are different questions -- a host converged a year
  ago runs whatever it was given then. `test_ansible_runtime_pin.py` holds the
  three declarations together.
- **The engine.** `catena-admin payload/cmd/catena-converge` copies the tree out
  of the image the panel service is RUNNING -- docker create plus docker cp into
  a root-owned 0700 stage dir -- and runs `playbooks/reconcile.yml` against an
  inventory it writes. Never out of the container's mirror: the panel has no
  root and no docker socket precisely so a compromise of it does not become host
  root.
- **The inventory it writes names the box by its own hostname.** Load-bearing,
  not cosmetic: four reconcile-side roles interpolate `inventory_hostname`, and
  `bootstrap/roles/common` sets the box's hostname from the operator's inventory name, so
  a converge run on the host and one run from a laptop arrive at the same
  strings. `localhost` would have produced different config for the same
  machine, silently. Same insight as the tunnel name.
- **The actions.** `catena-converge` and `catena-converge-status`, in the image
  drop-in, detached under `systemd-run` behind `flock /run/catena.lock`. The run
  action takes NO argument: a converge that accepted a playbook, a tag list or a
  limit from the panel would make the container the thing choosing what root
  executes.
- **The panel section**, under Settings beside the panel update. It withholds
  the button on an unreachable host and on one already converging, and it
  renders a paused host as paused rather than failed.

**The timer ships DISABLED, and that is the gate.** The unit pair
(`catena-converge-scheduled.service` / `.timer`) is installed by the payload
installer, which enables nothing -- that is what keeps install and activate two
observable steps. Enabling it needs a reconcile against an unchanged store that
reports zero changed tasks and restarts nothing, proven on the bench. Until
then a timer is a scheduled outage rather than a scheduled converge.

Note the unit is not called `catena-converge.service`: the panel dispatches an
ad-hoc converge through `systemd-run --unit catena-converge`, and a persistent
unit of that name would make every panel-triggered converge fail with a name
collision, on a host where the scheduled one had been working fine.

### Phase 5 and 6

Re-home the split unit owners (`catena-dashboard-sync.service` ships in the
image while its `.timer` is converge-rendered), unify the settings schema, and
retire `catena converge` to bootstrap and DR. Then optionally replace Ansible
with per-concern Go engines, following
`catena-cloudflared-sync` / `catena-dashboard-sync` / `catena-schedule`.

---

## Unresolved

Open questions and known conflicts, so the next person meets them here rather
than in the code.

- **Phase 1b is 186 files, and the gate is now partial rather than absent.**
  `test_every_role_reference_resolves.py` proves every role name and every
  literal `roles/<name>` path resolves, and that no name lives under two roots,
  so the mechanical half of the rename cannot land broken. What it cannot prove
  is that the split produces the same host. That is still an install from zero,
  so land the rename where a bench can run immediately after.
- **The settle assertion turns an invisible defect into a loud one.**
  `reconcile/roles/catena-admin` now re-inspects after a `docker service update` and fails
  if the same drift is still reported. If `swarm_service_drift` reads a field
  docker omits at its own default -- `StopGracePeriod` and the healthcheck
  fields are the candidates -- the first bench run FAILS the converge instead of
  quietly reporting changed for ever. That is the intended trade while nothing
  is in production, and it is the thing to expect if a bench run reds here.
- **The panel's release resolution runs on every converge, and FAILS the play
  when the registry does not answer.** Fine for a converge an operator started.
  Under phase 4's timer it becomes a scheduled dependency on GHCR being up. The
  fix is not caching: with `minimum=''` the store's pin already wins whenever
  there is one, so the answer is usually discarded anyway. Fail only when there
  is no usable pin.
- **The ACME seed is fill-only on a long-lived bench VM.** See the decision
  above. Fresh installs and rewinds re-seed; a re-converge after the bench
  host's bridge IP moved does not.
- **Twelve keys in the ops operator skeleton turn nothing.** The three
  `STACK_UPDATE_*`, six of the ten `AUTO_UPDATE_*`, `DEADMAN_TIMER`,
  `CATENA_APPS_SOURCE_VERSION`, `CATENA_WEBSITE_ENABLED`. Held in a declared
  table in ops `test_bench_writes_only_keys_catena_ce_reads.py` so the gate can
  refuse NEW ones while these are retired deliberately -- retiring them touches
  the bench config schema, so it wants a bench run behind it. The partially-wired
  auto-update lane is the one worth actually reading before deleting.

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
