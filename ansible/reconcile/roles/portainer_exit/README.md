# reconcile/roles/portainer_exit

Removes a Portainer left on the host once none of its stacks is left on it.

Client applications deploy from `/srv/catena/compose/<stack>/`. A Portainer
that still holds client stacks is emptied first: `catena-apps adopt -held`
(catena-admin `payload/engines/apps`) gives each stack a folder holding its
compose file, its Env as the `.env` and the catalog tag its file equals, and
redeploys it from there. A stack whose settings the folder writer refuses is
reported and stays on Portainer.

## Outcomes

- No Portainer service: nothing is adopted. What a removed Portainer leaves
  behind (data volume, admin-password secret, private network, public-port
  fragment) is removed.
- The pass ran, exited 0 and refused no stack: the service, its data volume,
  its admin-password secret, its private network and its public-port fragment
  are removed.
- The pass did not run, failed or refused a stack: Portainer keeps running and
  the converge prints why and which stacks need attention. The next converge
  tries again.

Either way the Traefik route of Portainer's sign-in gate and the swarm secrets
of that gate are removed; the gate itself is no longer deployed
(`oauth2_proxy`, whose `--prune` stack deploy removes the service).

The decision is the `portainer_exit_verdict` filter
(`playbooks/filter_plugins/portainer_exit.py`).

## Inputs

`defaults/main.yml` names the service, volume, secret, network and fragment of a
Portainer. Portainer's API key is read by `catena-apps` from the on-box store.
