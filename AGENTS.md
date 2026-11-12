# AGENTS.md

Instructions for AI agents (Claude Code, Codex, etc.) to follow.

- Do not modify SPEC.md
- Report any feature or code snippet not included in SPEC.md
- Report any item in SPEC.md that is not implemented.

## SPEC.md drafts

The owner edits SPEC.md; these rules shape the deltas you report.

- The editions part explains the licensing method only and links the website's edition comparison; it lists no
  feature per edition.
- Each row names its subject and the outcome a person sees, makes one claim, and never points at another row.

## Installer scope

- The installer and the inventory `.env` hold only what reaches and installs the host; Cloudflare, tailnet, backup
  and subdomain values are entered in the panel. The install never closes port 22.
- The installer stays Python.
- Roles and the installer stay provider-agnostic; provider-specific provisioning sits in its own layer.

## Config and secrets

- A new config or secret key lives in the on-box store, in the category `ansible/SECRETS.md` names: a client-supplied
  value in `ansible/helpers/knobs.yml`, a minted or user-held secret in `ansible/helpers/onbox_config.py`
  (`INTERNAL_SECRETS`, `USER_HELD_SECRETS`). No inventory secret, no vault.
- A store fact carries the `cfg_` prefix; a secret is recognised by its name (`_secret`, `_password`), never a
  `vault_` prefix.
- A knob's `label` and `help` are `{en, fr}` client copy; maintainer reasoning goes in YAML comments.

## Bootstrap and reconcile boundary

- Moving a role, value or action across `ansible/boundary.yml`: the side that reads a value owns it; a store key
  travels with the store, so a per-host value is derived on the host; the image ships every command it authorises;
  a dispatch body is removed when it moves.

## Default apps

- `default_apps` (`ansible/playbooks/group_vars/all/main.yml`) pins each default app to its newest catena-templates
  tag; move the pin when a tag lands.
- Core wires the default apps through what they declare, never an app-name branch; their addresses are set like
  every app's, never a Settings field.

## Sign-in and access

- Every gated address gets its own oauth2-proxy instance: Catena's own tools through an `oauth2_proxy_apps` entry,
  catalog apps from their `vps.auth.*` labels.
- A tailnet change extends the two forks of the tailscale role (Tailscale, Headscale); add no third control-plane
  product.
- A change that could lock the user out adds the new path, proves it end to end, and only then removes the old one.

## Ansible and swarm

- A block's `when` is re-evaluated for every task in it: decide into a fact before the block. Deleting a flag
  removes the blocks it guarded.
- Backup retention is the store's alone: this repo sets no default
  (`ansible/tests/unit/test_schedule_single_owner.py`).
- When collapsing two variable names, sweep templates as well as tasks: a deleted variable fails only at render.
- A container lookup asks for the swarm service-name label through `ansible/scripts/catena-container.sh`, never a
  compose label, a `vps.*` label or a name (`ansible/tests/unit/test_container_lookup_is_label_based.py`).
- A swarm secret's value never appears in an argv; a secret whose value can change carries its value's hash in its
  name (`ansible/playbooks/tasks/swarm_secrets.yml`).

## Validation

- Every role has a `tasks/validate.yml`; a role change updates it in the same change, with a negative check for
  what the role closes.
