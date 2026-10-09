# reconcile/roles/tier1_stack

The swarm services Catena creates directly: the control plane (traefik,
postgres, portainer) and coturn, the one app companion.
The service roles include two shared task files from here, and every converge
ends on this role's own render.

- `tasks/spec_one.yml` asks the `catena-tier1` host engine for one service's
  built-in spec (`catena-tier1 spec <name>`), then lifts its image to the pin
  the on-host update lane recorded when that pin is newer.
- `tasks/reconcile_one.yml` passes one spec to `catena-tier1 apply` on stdin;
  the engine creates the service, or updates whatever differs from the spec.
- `tasks/main.yml` turns every spec the roles collected into
  `/etc/catena/stacks/tier1.yml` (the control plane) and `companions.yml`
  (coturn), and loads each file with `docker stack config`, which type-checks
  the specs against the docker this host runs. The services themselves come
  from `reconcile_one.yml`.

## Inputs

- Defaults: `tier1_engine_bin` (`/usr/local/bin/catena-tier1`),
  `tier1_stack_dir` (`/etc/catena/stacks`), `tier1_stack_path` and
  `tier1_companions_path` (the two rendered files),
  `tier1_stack_required`, the three control-plane services `tier1.yml` waits
  for, and `tier1_companion_services`, the companions (coturn) whose presence
  keeps `companions.yml`.
- Include vars: `tier1_spec_name` plus an optional `tier1_spec_args` argv for
  `spec_one.yml`, which answers in `tier1_builtin_spec`; `tier1_spec` for
  `reconcile_one.yml`.
- Facts: `catena_tier1_specs` and `catena_companion_specs`, the lists each
  service role adds its spec to; `catena_image_pins`, set by
  `catena-ce/ansible/playbooks/tasks/load_onbox_config.yml`.
- Filters: `tier1_spec_upsert` and `tier1_stack_render`
  (`catena-ce/ansible/playbooks/filter_plugins/tier1_stack.py`),
  `catena_image_pin` (`catena-ce/ansible/playbooks/filter_plugins/image_pin.py`).

## Side effects

- Both shared task files stop the converge when the engine binary is missing.
- `reconcile_one.yml` creates or updates one swarm service, and reports a
  change when the engine's summary line counts one.
- `main.yml` creates `/etc/catena/stacks` (0750 root) and writes `tier1.yml`
  (0640 root) once traefik, postgres and portainer have all contributed. A
  tag-scoped converge that ran fewer of them names the missing ones and keeps
  the previous file. `companions.yml` is written when coturn contributed,
  deleted when nothing did and coturn does not run, and kept when coturn runs
  without contributing.
- `main.yml` runs at the end of every converge, from
  `catena-ce/ansible/playbooks/tasks/converge_tail.yml`, which both
  `converge.yml` (the install) and `reconcile.yml` (the converge a host runs on
  itself) include. Both reach the two shared task files through the service
  roles.

## Related

- Callers: the `traefik`, `postgres` and `portainer` roles (control plane)
  and `coturn` (companion), beside this one in
  `catena-ce/ansible/reconcile/roles/`.
- The engine: `catena-admin/payload/engines/tier1`, its catalog and its
  inspect, create and update steps; the `payload` role installs it.
- The `infrastructure` role creates `/etc/catena/stacks` too, with the same
  mode, for the app stack files.
- `catena-ce/ansible/boundary.yml` lists this role under `post_task_roles`.
- Tests: `catena-ce/ansible/tests/unit/test_tier1_declaration.py`,
  `catena-ce/ansible/tests/unit/test_tier1_stack_render.py`.
