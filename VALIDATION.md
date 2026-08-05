<!-- AUTOGEN by `audit --write-public-specs` from the maintainers'
     audit manifest (features.yml) crossed with the live rehearsal-
     scenario mapping and this repository's CI workflows. Do not
     hand-edit; the maintainers' CI regenerates and fails on any
     drift (`audit --check-public-specs`). -->


# Validation

This sheet is **generated, not written**: it is rendered from the same machine-checked manifest that classifies every source file and every test scenario of the Catena product, and the maintainers' CI fails whenever this file drifts from that manifest. What it claims is what is enforced.

Catena is exercised end-to-end by an automated rehearsal suite: each scenario provisions disposable virtual machines, drives the real product (install, converge, back up, break, restore) and asserts the outcome -- including deliberate failure injection.

Two numbers are reported everywhere: **rehearsed** means the scenario was last recorded PASSING on a real run, and is the only number treated as evidence; **declared** additionally counts scenarios that exist but have not been observed passing yet. A scenario is never counted for merely existing.

**Coverage: 10 Community features, 78 of 84 rehearsal scenarios observed passing; 12 Catena Pro features, 39 of 42; plus 17 of 23 maintainer-internal rehearsals.**

## Community features (this repository)

### Encrypted backups to client-owned storage

A scheduled weekly backup plus manual backups any time. Backups are encrypted on the server before leaving it and land in object storage the client owns; snapshots can be listed, browsed and exported without a restore. Daily and sub-daily cadence is a Catena Pro feature.

Implemented in: `ansible/playbooks/backup_now.yml`, `ansible/playbooks/filter_plugins/backup_cadence_cap.py`, `ansible/roles/backup`, `ansible/scripts/catena-restic-key.py`, `ansible/scripts/disk-preflight.sh`

Rehearsal scenarios (11 of 11 observed passing): `backup_rollback`, `backup_schedule_applied`, `concurrent_backup_lock_contention`, `fi_b2_pg_dump_failed`, `fi_b3_snapshot_id_mismatch`, `fi_b4_locked_pack_rotation`, `fi_b6_healthchecks_down`, `fi_b7_ntfy_delivery_fails`, `malformed_catalog_rejection`, `restic_password_rotation_round_trip`, `snapshot_export_round_trip`

### Single sign-on across the suite

One account signs in to every application, with per-application access control and staff/administrator separation enforced in front of the applications, not inside each one.

Implemented in: `ansible/roles/keycloak`, `ansible/roles/oauth2_proxy`, `ansible/scripts/catena-keycloak-realm-export.sh`, `ansible/scripts/clients_provisioner.py`, `ansible/scripts/dashboard-sync.py`, `ansible/scripts/gate_routes.py`, `ansible/scripts/keycloak_client.py`, `ansible/scripts/route_synth.py`

Rehearsal scenarios (10 of 10 observed passing): `fi_a1_realm_marker_collision`, `fi_a2_oidc_secret_rotation`, `fi_a3_keycloak_unreachable`, `fi_a4_master_realm_idempotent`, `fi_a5_wrong_group_assignment`, `keycloak_admin_email_loss_recovery`, `keycloak_signing_keys_rotation_round_trip`, `oauth2_proxy_cookie_rotation_round_trip`, `user_recovery_2fa_reset`, `user_recovery_kcadm_temp_password`

### Administration dashboard

A web dashboard with role-aware access (staff see status, administrators also get maintenance actions). Every action a button triggers is logged in the server's system journal.

Implemented in: `ansible/roles/catena-admin`, `ansible/scripts/catena-admin-runner.sh`

Rehearsal scenarios (5 of 5 observed passing): `audit_chain_tamper_evident`, `ce_admin_actions`, `ce_admin_smoke`, `quiesce_resume_round_trip`, `wizard_restore_smoke`

### Installation and application deployment

Prepares a fresh server, installs the platform, and deploys the selected applications. Re-running the same managed operation converges the server back to its declared configuration, so a drifted or half-configured server is repaired, not rebuilt by hand.

Implemented in: `ansible/catena_cli.py`, `ansible/helpers/*.py`, `ansible/playbooks/bootstrap.yml`, `ansible/playbooks/preflight.yml`, `ansible/playbooks/show_dr_keyset.yml`, `ansible/playbooks/site.yml`, `ansible/playbooks/tasks/load_onbox_config.yml`, `ansible/playbooks/uninstall.yml`, `ansible/roles/common`, `ansible/roles/docker`, `ansible/roles/host_hardening`, `ansible/roles/payload`, `ansible/roles/portainer`, `ansible/roles/postgres`, `ansible/roles/storage`, `ansible/roles/tier1_stack`, `ansible/roles/traefik`, `ansible/seed.py`

Rehearsal scenarios (15 of 16 observed passing): `ce_converge`, `ce_install_suite`, `ce_uninstall`, `converge_modify`, `fi_c1_docker_daemon_hang`, `fi_c3_portainer_crash_mid_deploy`, `fi_c4_registry_pull_timeout`, `fi_c6_cloudflared_flapping`, `fi_c7_coturn_cert_expired`, `fi_c8_nextcloud_init_loop`, `fi_u1_compose_lint_reject`, `mixed_template_negative_restore`, `repair_broken_template_round_trip`, `scheduler_easyappointments`, `swarm_overlay_selfheal`; declared, not yet observed passing: `dev_to_prod_cutover_round_trip`

### Application catalog and suite integrations

Per-application deployment plus the wiring that makes the suite feel like one product: email, chat and video calling, file/office integration, antivirus watch and delivery canaries.

Implemented in: `ansible/roles/infrastructure`, `ansible/scripts/nextcloud-talk-hpb-wire.sh`, `ansible/scripts/rocketchat-jitsi-wire.sh`, `ansible/scripts/run-clamav-watch.sh`, `ansible/scripts/run-mail-canary.sh`, `ansible/scripts/wire-nextcloud-*.sh`

Rehearsal scenarios (2 of 2 observed passing): `mailserver_round_trip`, `nextcloud_versions_retention_applied`

### Self-hosted monitoring

On-server status pages, resource monitoring, a disk-space watchdog and an always-fresh report of which installed applications have updates available -- all hosted on the client's own server.

Implemented in: `ansible/scripts/beszel-hc-shim.py`, `ansible/scripts/beszel-seed.py`, `ansible/scripts/catena-version-check.py`, `ansible/scripts/gatus-sync.py`

Rehearsal scenarios (1 of 1 observed passing): `fi_u3_gatus_baseline_down`

### Private networking and hardened public access

All web traffic reaches the server through an encrypted tunnel, so no web port is ever open on the machine itself; remote administration rides a private peer-to-peer network, and audio/video calls get their own dedicated relay.

Implemented in: `ansible/helpers/public_ports.py`, `ansible/playbooks/regenerate-cf-tunnel.yml`, `ansible/playbooks/rotate-tailscale.yml`, `ansible/roles/cloudflare_tunnel`, `ansible/roles/cloudflare_tunnel_regenerate`, `ansible/roles/coturn`, `ansible/roles/tailscale`, `ansible/scripts/catena-public-ports.py`

Rehearsal scenarios (13 of 16 observed passing): `ce_install_headscale`, `cf_activate`, `cf_tunnel_regenerate_round_trip`, `fi_n10_multidomain_cap`, `fi_n1_tailnet_partition_mid_converge`, `fi_n2_cf_tunnel_down`, `fi_n3_dns_propagation_lag`, `fi_n4_cf_zone_misconfigured`, `fi_n5_provider_outage_mid_restore`, `fi_n6_s3_endpoint_5xx`, `fi_n7_restic_repo_unreachable`, `fi_n8_ufw_concurrent_ssh`, `fi_n9_public_ip_change`; declared, not yet observed passing: `cloudflare_api_rotation_round_trip`, `fi_v3_tailscale_acl_misconfig`, `tailscale_oauth_rotation_round_trip`

### Disaster recovery and restore

A whole server can be rebuilt from nothing but the backup endpoint and its key, and a live server can be restored in place. Databases and applications come back as one coordinated operation, consistent with each other rather than each from its own moment in time. Both paths are rehearsed continuously, including across operating-system and database major versions.

Implemented in: `ansible/playbooks/restore.yml`

Rehearsal scenarios (16 of 18 observed passing): `ce_restore`, `fi_d2_pg_dumpall_replay_constraint`, `fi_d3_postgres_oom_mid_restore`, `fi_d4_disk_full_mid_snapshot`, `fi_d5_disk_full_mid_converge`, `fi_d6_volume_uid_drift`, `fi_d7_restic_corrupt_pack`, `nc_s3_hot_recovery`, `nc_sync_wipe_restore`, `pitr_fuse_round_trip`, `recover_secrets_from_running_host`, `recovery_landing_page_bilingual_parity`, `restore_dr`, `restore_version_skew_abort`, `s3_reconcile_orphan_cleanup`, `selective_restore_round_trip`; declared, not yet observed passing: `debian_major_upgrade_restore`, `pg_major_version_cross_restore`

### No lock-in, ever

Delete the admin panel and everything else keeps working: backups run, restores work, and every application stays online, using only standard tools and the settings stored on the server itself. Leaving costs convenience, never data.

Implemented in: `ansible/roles/backup`

Rehearsal scenarios (2 of 2 observed passing): `recovery_readme_manual_restore`, `sovereign_exit`

### Automated health and exposure checks

A validation pass proves both directions: every service answers where it should (on-server and through the private network), and an external scan confirms nothing is reachable that should not be.

Implemented in: `ansible/playbooks/validate.yml`, `ansible/scripts/verify_gated_intent.py`, `ansible/tests/external/scan_ports.py`

Rehearsal scenarios (3 of 3 observed passing): `ce_validate`, `fi_v2_external_scan_blocked`, `security_scan`

## Catena Pro features

Catena Pro features are not built from this repository. They ship in the public, signed `ghcr.io/catenahq/catena-admin` image -- pullable anonymously and published with a CycloneDX component inventory, so what runs on a server can be scanned without asking anyone for access.

They are exercised by the same rehearsal suite as the Community features above, and the counts below come from the same manifest. Implementation paths and scenario names are the part that stays unpublished.

| Feature | Rehearsed | Declared |
| --- | --- | --- |
| Signed monthly compliance attestation | 1 | 1 |
| Tamper-evident central audit trail | 1 | 1 |
| Offsite immutable backup copy | 6 | 6 |
| Vulnerability scanning | 1 | 1 |
| Automated daily maintenance | 13 | 13 |
| Managed lifecycle operations (migration, decommission) | see note | see note |
| Licensed feature activation | 6 | 6 |
| Identity posture monitoring | 1 | 1 |
| Managed updates with automatic rollback | 9 | 9 |
| External availability monitoring | 0 | 1 |
| Multiple domains, each with its own private sign-on | 1 | 1 |
| A move that can be called off | 2 | 4 |

Note: features marked *see note* are maintainer-run procedures; their rehearsals are counted under maintainer-internal tooling below.

## Maintainer-internal tooling

The maintainers' internal tooling (test harness, control plane, rotation and maintenance utilities) accounts for a further 3 features and 17 of 23 rehearsal scenarios observed passing; details stay private.

## Continuous integration gates on this repository

Parsed from the committed workflow files; every job below runs on each change.

| Workflow | File | Jobs |
| --- | --- | --- |
| CI | `.github/workflows/ci.yml` | `installer`, `duplication` |
| security | `.github/workflows/security.yml` | `scanctl` |
| Trivy | `.github/workflows/trivy.yml` | `resolve-pins-matrix`, `trivy-pinned-images`, `trivyignore-expiry`, `trivy-gate` |

See [SPEC.md](SPEC.md) for what this repository promises and the machine-checked invariants behind each promise.
