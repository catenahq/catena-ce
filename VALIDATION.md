<!-- AUTOGEN by `audit --write-public-specs` from the maintainers'
     audit manifest (features.yml) crossed with the live rehearsal-
     scenario mapping and this repository's CI workflows. Do not
     hand-edit; the maintainers' CI regenerates and fails on any
     drift (`audit --check-public-specs`). -->


# Validation

This sheet is **generated, not written**: it is rendered from the same machine-checked manifest that classifies every source file and every test scenario of the Catena product, and the maintainers' CI fails whenever this file drifts from that manifest. What it claims is what is enforced.

Catena is exercised end-to-end by an automated rehearsal suite: each scenario provisions disposable virtual machines, drives the real product (install, converge, back up, break, restore) and asserts the outcome -- including deliberate failure injection. The counts below are those rehearsals.

**Coverage: 10 Community features exercised by 95 rehearsal scenarios; 13 Catena Pro features by 44; plus 24 maintainer-internal rehearsals.**

## Community features (this repository)

### Encrypted backups to storage you own

A scheduled weekly backup plus manual backups any time. Backups are encrypted on the server before leaving it and land in object storage the client owns; snapshots can be listed, browsed and exported without a restore. Daily and sub-daily cadence is a Catena Pro feature.

Implemented in: `ansible/playbooks/backup_now.yml`, `ansible/playbooks/filter_plugins/backup_tier.py`, `ansible/roles/backup`, `ansible/scripts/backup-coverage.sh`, `ansible/scripts/catena-restic-key.py`, `ansible/scripts/restic-env.sh`, `ansible/scripts/restic-mount.sh`, `ansible/scripts/restic-short-id.sh`, `ansible/scripts/restic-unmount.sh`, `ansible/scripts/run-backup.sh`, `ansible/scripts/snapshot-export.sh`, `ansible/scripts/snapshot-list.sh`

Rehearsal scenarios (12): `backup_rollback`, `backup_tier_schedule_resolution`, `concurrent_backup_lock_contention`, `fi_b2_pg_dump_failed`, `fi_b3_snapshot_id_mismatch`, `fi_b4_locked_pack_rotation`, `fi_b5_s3_quota_exhausted`, `fi_b6_healthchecks_down`, `fi_b7_ntfy_delivery_fails`, `malformed_catalog_rejection`, `restic_password_rotation_round_trip`, `snapshot_export_round_trip`

### Single sign-on across the suite

One account signs in to every application, with per-application access control and staff/administrator separation enforced in front of the applications, not inside each one.

Implemented in: `ansible/roles/keycloak`, `ansible/roles/oauth2_proxy`, `ansible/scripts/catena-keycloak-realm-export.sh`, `ansible/scripts/clients_provisioner.py`, `ansible/scripts/dashboard-sync.py`, `ansible/scripts/gate_routes.py`, `ansible/scripts/keycloak_client.py`, `ansible/scripts/route_synth.py`

Rehearsal scenarios (10): `fi_a1_realm_marker_collision`, `fi_a2_oidc_secret_rotation`, `fi_a3_keycloak_unreachable`, `fi_a4_master_realm_idempotent`, `fi_a5_wrong_group_assignment`, `keycloak_admin_email_loss_recovery`, `keycloak_signing_keys_rotation_round_trip`, `oauth2_proxy_cookie_rotation_round_trip`, `user_recovery_2fa_reset`, `user_recovery_kcadm_temp_password`

### Administration dashboard

A web dashboard with role-aware access (staff see status, administrators also get maintenance actions). Every action a button triggers is logged in the server's system journal.

Implemented in: `ansible/roles/catena-admin`, `ansible/scripts/catena-admin-runner.sh`

Rehearsal scenarios (12): `admin_apps_tab_admin_full`, `admin_apps_tab_staff_filtered`, `admin_staff_cannot_reach_admin_routes`, `audit_db_torn_write_recovery`, `catena_admin_actions_round_trip`, `catena_admin_daily_tab_renders`, `catena_admin_resources_tab_links_beszel`, `catena_admin_smoke`, `ce_admin_actions`, `ce_admin_smoke`, `quiesce_resume_round_trip`, `wizard_restore_smoke`

### Installation and application deployment

Prepares a fresh server, installs the platform, and deploys the selected applications. Re-running the same managed operation converges the server back to its declared configuration, so a drifted or half-configured server is repaired, not rebuilt by hand.

Implemented in: `ansible/catena_cli.py`, `ansible/helpers/*.py`, `ansible/playbooks/bootstrap.yml`, `ansible/playbooks/preflight.yml`, `ansible/playbooks/show_dr_keyset.yml`, `ansible/playbooks/site.yml`, `ansible/playbooks/tasks/load_onbox_config.yml`, `ansible/playbooks/uninstall.yml`, `ansible/roles/common`, `ansible/roles/docker`, `ansible/roles/host_hardening`, `ansible/roles/portainer`, `ansible/roles/postgres`, `ansible/roles/storage`, `ansible/roles/traefik`, `ansible/seed.py`

Rehearsal scenarios (17): `ce_converge`, `ce_install`, `ce_install_suite`, `ce_uninstall`, `converge_modify`, `dev_to_prod_cutover_round_trip`, `fi_c1_docker_daemon_hang`, `fi_c3_portainer_crash_mid_deploy`, `fi_c4_registry_pull_timeout`, `fi_c6_cloudflared_flapping`, `fi_c7_coturn_cert_expired`, `fi_c8_nextcloud_init_loop`, `fi_u1_compose_lint_reject`, `fresh_install`, `mixed_template_negative_restore`, `repair_broken_template_round_trip`, `scheduler_easyappointments`

### Application catalog and suite integrations

Per-application deployment plus the wiring that makes the suite feel like one product: email, chat and video calling, file/office integration, antivirus watch and delivery canaries.

Implemented in: `ansible/roles/infrastructure`, `ansible/scripts/nextcloud-talk-hpb-wire.sh`, `ansible/scripts/rocketchat-jitsi-wire.sh`, `ansible/scripts/run-clamav-watch.sh`, `ansible/scripts/run-mail-canary.sh`, `ansible/scripts/wire-nextcloud-*.sh`

Rehearsal scenarios (2): `mailserver_round_trip`, `nextcloud_versions_retention_applied`

### Self-hosted monitoring

On-server status pages, resource monitoring, a disk-space watchdog and an always-fresh report of which installed applications have updates available -- all hosted on the client's own server.

Implemented in: `ansible/scripts/beszel-hc-shim.py`, `ansible/scripts/beszel-seed.py`, `ansible/scripts/catena-version-check.py`, `ansible/scripts/gatus-sync.py`, `ansible/scripts/run-disk-watch.sh`

Rehearsal scenarios (1): `fi_u3_gatus_baseline_down`

### Private networking and hardened public access

All web traffic reaches the server through an encrypted tunnel, so no web port is ever open on the machine itself; remote administration rides a private peer-to-peer network, and audio/video calls get their own dedicated relay.

Implemented in: `ansible/helpers/public_ports.py`, `ansible/playbooks/filter_plugins/catena_multidomain.py`, `ansible/playbooks/regenerate-cf-tunnel.yml`, `ansible/playbooks/rotate-tailscale.yml`, `ansible/roles/cloudflare_tunnel`, `ansible/roles/cloudflare_tunnel_regenerate`, `ansible/roles/coturn`, `ansible/roles/tailscale`, `ansible/scripts/catena-public-ports.py`

Rehearsal scenarios (17): `ce_install_headscale`, `ce_install_tailnet`, `cf_activate`, `cf_tunnel_regenerate_round_trip`, `cloudflare_api_rotation_round_trip`, `fi_n10_multidomain_cap`, `fi_n1_tailnet_partition_mid_converge`, `fi_n2_cf_tunnel_down`, `fi_n3_dns_propagation_lag`, `fi_n4_cf_zone_misconfigured`, `fi_n5_provider_outage_mid_restore`, `fi_n6_s3_endpoint_5xx`, `fi_n7_restic_repo_unreachable`, `fi_n8_ufw_concurrent_ssh`, `fi_n9_public_ip_change`, `fi_v3_tailscale_acl_misconfig`, `tailscale_oauth_rotation_round_trip`

### Disaster recovery and restore

A whole server can be rebuilt from nothing but the backup endpoint and its key, and a live server can be restored in place. Databases and applications come back as one coordinated operation, consistent with each other rather than each from its own moment in time. Both paths are rehearsed continuously, including across operating-system and database major versions.

Implemented in: `ansible/playbooks/restore.yml`

Rehearsal scenarios (19): `ce_restore`, `debian_major_upgrade_restore`, `fi_d2_pg_dumpall_replay_constraint`, `fi_d3_postgres_oom_mid_restore`, `fi_d4_disk_full_mid_snapshot`, `fi_d5_disk_full_mid_converge`, `fi_d6_volume_uid_drift`, `fi_d7_restic_corrupt_pack`, `hot_restore_round_trip`, `nc_s3_hot_recovery`, `nc_sync_wipe_restore`, `pg_major_version_cross_restore`, `pitr_fuse_round_trip`, `recover_secrets_from_running_host`, `recovery_landing_page_bilingual_parity`, `restore_dr`, `restore_version_skew_abort`, `s3_reconcile_orphan_cleanup`, `selective_restore_round_trip`

### No lock-in, ever

Delete the admin panel and everything you own keeps working: backups run, restores work, and every application stays online, using only standard tools and the settings stored on your own server. Leaving costs you convenience, never your data.

Implemented in: `ansible/roles/backup`, `ansible/scripts/run-backup.sh`, `ansible/scripts/snapshot-export.sh`, `ansible/scripts/snapshot-list.sh`

Rehearsal scenarios (2): `recovery_readme_manual_restore`, `sovereign_exit`

### Automated health and exposure checks

A validation pass proves both directions: every service answers where it should (on-server and through the private network), and an external scan confirms nothing is reachable that should not be.

Implemented in: `ansible/playbooks/validate.yml`, `ansible/scripts/verify_gated_intent.py`, `ansible/tests/external/scan_ports.py`

Rehearsal scenarios (3): `ce_validate`, `fi_v2_external_scan_blocked`, `security_scan`

## Catena Pro features (private repository)

Catena Pro features are exercised by the same rehearsal suite. Implementation paths and scenario names stay private; the counts are generated from the same manifest as the Community section above.

| Feature | Rehearsal scenarios |
| --- | --- |
| Signed monthly compliance attestation | 1 |
| Tamper-evident central audit trail | 1 |
| Offsite immutable backup copy | 6 |
| Vulnerability scanning | 1 |
| Automated daily maintenance | 14 |
| Managed lifecycle operations (migration, decommission) | see note |
| Licensed feature activation | 4 |
| Identity posture monitoring | 1 |
| Managed updates with automatic rollback | 9 |
| External availability monitoring | 1 |
| Multiple domains, each with its own private sign-on | 1 |
| Per-user mail and file archiving | 3 |
| A move you can call off | 4 |

Note: features marked *see note* are maintainer-run procedures; their rehearsals are counted under maintainer-internal tooling below.

## Maintainer-internal tooling

The maintainers' internal tooling (test harness, control plane, rotation and maintenance utilities) accounts for a further 3 features and 24 rehearsal scenarios; details stay private.

## Continuous integration gates on this repository

Parsed from the committed workflow files; every job below runs on each change.

| Workflow | File | Jobs |
| --- | --- | --- |
| CI | `.github/workflows/ci.yml` | `installer`, `duplication` |
| security | `.github/workflows/security.yml` | `scanctl` |
| Trivy | `.github/workflows/trivy.yml` | `resolve-matrix`, `trivy-operator-stack-pins`, `trivyignore-expiry`, `trivy-gate` |

See [SPEC.md](SPEC.md) for what this repository promises and the machine-checked invariants behind each promise.
