<!-- AUTOGEN by `audit --write-public-specs` from the test bench's
     scenario headers. Do not hand-edit; the maintainers' CI
     regenerates and fails on any drift
     (`audit --check-public-specs`). -->

# Test scenarios

The 164 scenarios the maintainers' test bench carries, and the behaviour each one is written to prove against a real virtual machine. Names are stable: a SPEC.md invariant citing `bench:<name>` refers to the row of the same name below.

| Scenario | What it proves |
| --- | --- |
| `activate_ee` | A Community host becomes a licensed Pro host when a valid offline licence token is installed, and unlocks nothing without one. |
| `admin_action_unknown_rejected` | The administrative action dispatcher refuses a request for an action it does not carry, exits non-zero, and names nothing it might have run. |
| `app_restore_round_trip` | Restoring one application returns it to its snapshot content and leaves every other application on the host untouched. |
| `audit_chain_tamper_evident` | An exported administrative audit trail verifies away from the host it came from, and stops verifying as soon as any row is altered. |
| `backup_rollback` | A file changed after a backup is returned to its snapshot content by a rollback, and the applications come back with it. |
| `backup_schedule_applied` | The scheduled maintenance a host actually runs matches what its configuration store asks for, and a freshly converged host schedules nothing at all. |
| `catena_admin_self_update` | The administration panel updates both halves of itself, the container and the engines it installs on the host, and rolls both back together when the new version is bad. |
| `ce_admin_actions` | Pressing a maintenance button in the Community administration panel dispatches the action and streams its result back. |
| `ce_admin_smoke` | A Community host carries the host-side state its administration panel needs, and the panel answers. |
| `ce_backup_deferred` | A host with no scheduled backup can still take one on demand, because whether a host backs up is answered by its storage credentials rather than by a switch. |
| `ce_converge` | Applying the Community installer a second time to an already converged host changes nothing. |
| `ce_install_headscale` | A host joins a self-hosted private network control server instead of the hosted one, and reaches the rest of the network through it. |
| `ce_install_no_tailnet` | A server installs with no private network and no domain token, keeps port 22 open, takes its token later, and refuses to close 22. |
| `ce_install_suite` | A Community install brings up the full application suite rather than the base host alone. |
| `ce_restore` | A Community host restores from its encrypted backup and comes back with the content the snapshot held. |
| `ce_uninstall` | Uninstalling hands the host back to the operating system, including the package-update timers the install had taken over. |
| `ce_validate` | A converged Community host validates itself end to end using only the shipped installer. |
| `cf_tunnel_regenerate_round_trip` | Rotating the public tunnel mints a new one, repoints the public name at it, revokes the old one, and rolls the connector onto the new credential. |
| `cloudflare_api_rotation_round_trip` | The edge-provider API credential is rotated on the planned cycle and the host keeps serving across the change. |
| `concurrent_backup_lock_contention` | Two maintenance jobs contending for the host lock run one after the other instead of at the same time. |
| `container_delete_recreated` | A container deleted out from under the orchestrator, and a whole service deleted with it, are scheduled again and come back serving. |
| `control_plane_update_rollback` | A bad version bump of a control-plane service is detected, rolled back automatically, and quarantined so it is never retried blindly. |
| `converge_modify` | Rotating a secret in the host configuration store and re-converging propagates the new value into every service that uses it. |
| `converge_preserves_bumped_image` | An image version bumped on the host survives the next converge instead of being reverted to the shipped default. |
| `cve_residual_emits_findings` | The vulnerability scan produces a well-formed machine-readable report on a real host, whether or not it finds anything. |
| `daily_chain_container_rollback` | A failed container update inside the daily maintenance chain rolls that service back and lets the chain continue. |
| `daily_chain_full_pass` | The daily maintenance chain runs every stage end to end on a real host and reports itself idle when it finishes. |
| `daily_chain_preflight_aborts_low_disk` | The daily maintenance chain refuses to start when free disk at the staging area is below the configured floor. |
| `daily_chain_quiesce_invoked` | The daily maintenance chain quiesces applications before taking a backup and always releases them afterwards, including when the backup aborts the chain. |
| `daily_chain_verify_cold_blocks_mirror` | A failed cold-storage verification stops the offsite copy from pushing, so a bad archive is never propagated. |
| `daily_chain_verify_cold_fail_configurable` | Whether a failed cold-storage verification blocks the update tail of the daily chain follows the host configuration rather than being hard- wired. |
| `daily_chain_verify_hot_fail_aborts_updates` | A failed backup verification aborts the update tail of the daily chain, preserving a known-good rollback target. |
| `daily_state_corrupt_fallback` | A corrupted daily maintenance state file fails the chain with a readable diagnostic, and the reset command returns the host to the start of the chain. |
| `daily_umbrella_healthchecks` | The daily maintenance chain signals an external monitor when it starts and again when it succeeds. |
| `debian_major_upgrade_restore` | A snapshot taken on one major operating-system release restores onto a host running the next one. |
| `decommission` | Decommissioning tears a host down cleanly, releasing every external resource it held. |
| `decommission_recovery` | A decommissioned host is rebuilt from the archival snapshot its decommission left behind. |
| `dev_to_prod_cutover_round_trip` | A staging deployment is promoted to its production hostname on the same machine, with every application reconciled to the new address. |
| `ee_attest` | A signed monthly compliance attestation is produced from a host's own evidence and verifies against the published key. |
| `ee_audit_ship` | Administrative audit events shipped from a host arrive intact at the central collector. |
| `ee_ce_regression` | Community actions keep working on a host that has been licensed for Pro. |
| `ee_daily_cycle` | The daily maintenance engine drives a full chain on a licensed host, stage by stage, to completion. |
| `ee_entitlement_partial` | A licence naming some panels unlocks exactly those, and the actions behind the panels it leaves out are absent rather than merely hidden. |
| `ee_identity_probe` | The identity posture probe reports how single sign-on is configured on a host and changes verdict when that configuration changes. |
| `ee_lapse` | A host whose licence lapses freezes its licensed features while everything Community keeps working. |
| `ee_multidomain` | A second, unrelated domain is attached to a licensed host and both domains are served through the one tunnel. |
| `ee_named_buttons` | Each licensed maintenance button dispatches its own distinct action rather than sharing one. |
| `fi_a1_realm_marker_collision` | A single sign-on realm that has already been imported is left alone by the next converge. |
| `fi_a2_oidc_secret_rotation` | Rotating the single sign-on client secret propagates to both the identity provider and the proxy in one converge. |
| `fi_a3_keycloak_unreachable` | An unreachable identity provider is reported as a failed readiness gate rather than passing silently. |
| `fi_a4_master_realm_idempotent` | A second converge of the identity provider bootstrap changes nothing. |
| `fi_a5_wrong_group_assignment` | A user outside the administrator group is refused at the authenticating proxy, before any request reaches the application behind it. |
| `fi_b2_pg_dump_failed` | A failed database dump aborts the backup before any archive is written, so no partial snapshot is ever created. |
| `fi_b3_snapshot_id_mismatch` | A restore asking for a snapshot that does not exist is refused with an error naming what was asked for. |
| `fi_b4_locked_pack_rotation` | Backup data held under an object-lock retention policy survives an attempt to prune it away. |
| `fi_b6_healthchecks_down` | An unreachable monitoring endpoint makes the fallback alert channel fire instead of the failure going unnoticed. |
| `fi_b7_ntfy_delivery_fails` | A failed alert delivery leaves a record on the host and does not block the work that raised it. |
| `fi_c1_docker_daemon_hang` | A frozen container daemon makes the converge fail within a bounded time rather than hanging indefinitely. |
| `fi_c3_portainer_crash_mid_deploy` | A crash of the deployment control plane during a deploy is covered by the orchestrator restarting it, and the deployment completes. |
| `fi_c4_registry_pull_timeout` | Heavy packet loss on the host outbound network makes the converge fail fast at its first external step. |
| `fi_c6_cloudflared_flapping` | Re-converging the public tunnel updates its configuration without restarting a healthy connector. |
| `fi_c7_coturn_cert_expired` | An expired media-relay certificate is renewed automatically by the scheduled renewal timer and its deploy hook. |
| `fi_c8_nextcloud_init_loop` | An application whose environment has been corrupted fails visibly with captured diagnostics rather than looping in silence. |
| `fi_d2_pg_dumpall_replay_constraint` | Replaying a database dump recreates a database role the cluster has lost. |
| `fi_d3_postgres_oom_mid_restore` | A restore that exhausts the host disk fails cleanly rather than leaving a half-loaded database. |
| `fi_d4_disk_full_mid_snapshot` | A backup that runs out of disk partway through fails loudly and writes no half-finished archive. |
| `fi_d5_disk_full_mid_converge` | A converge on a host with too little free disk aborts at its preflight check before pulling anything. |
| `fi_d6_volume_uid_drift` | A service whose user has drifted away from what it requires is reconciled back by the next converge. |
| `fi_d7_restic_corrupt_pack` | Corruption introduced into the backup repository is detected by the deep verification pass. |
| `fi_m1_target_unreachable` | A migration whose destination host is unreachable aborts before it stops anything on the source. |
| `fi_m2_cross_provider_proxy` | A host migrates across hosting providers and regions, not only between machines at one of them. |
| `fi_n10_multidomain_cap` | A Community host asked to serve more than one domain degrades to the one it supports rather than refusing to converge. |
| `fi_n1_tailnet_partition_mid_converge` | A private network partition during a converge fails the reachability probe instead of proceeding blind. |
| `fi_n2_cf_tunnel_down` | Blocked access to the edge provider at validation time is reported as a warning rather than stopping the converge. |
| `fi_n3_dns_propagation_lag` | Slow public name propagation is absorbed by the certificate retry budget, and issuance still completes. |
| `fi_n4_cf_zone_misconfigured` | A wrong edge-provider credential or zone aborts the converge immediately with a clear failure. |
| `fi_n5_provider_outage_mid_restore` | A network outage during a restore aborts it loudly, and the restore resumes cleanly once the network returns. |
| `fi_n6_s3_endpoint_5xx` | An object store that stops answering surfaces as a backup error rather than as a silent success. |
| `fi_n7_restic_repo_unreachable` | An unreachable backup repository stops the backup before it prunes anything. |
| `fi_n8_ufw_concurrent_ssh` | An administrative session survives the host firewall lockdown being applied underneath it. |
| `fi_n9_public_ip_change` | A change to the host public address is re-rendered into the media relay configuration by the next converge. |
| `fi_o2_operator_ctrl_c` | A converge interrupted partway through leaves nothing broken, and re- running it completes the work. |
| `fi_o4_hosts_yml_malformed` | A malformed inventory file fails with a readable parse error naming the offending line. |
| `fi_o5_deadman_off_by_default` | The external dead-man watchdog installs nothing at all when no endpoint is configured. |
| `fi_s2_input_truncated` | A truncated installation input file is rejected before anything is provisioned. |
| `fi_s3_placeholder_inert` | An unreplaced placeholder left in a lower-precedence configuration file has no effect on a converged host. |
| `fi_s4_tailscale_oauth_revoked` | A revoked private network credential is detected up front by minting a throwaway key, rather than at converge time. |
| `fi_s6_ovh_token_invalid` | An invalid hosting-provider credential aborts snapshot creation instead of reporting success. |
| `fi_s7_smtp_creds_invalid` | Invalid outbound mail credentials surface as an authentication failure from a live probe at the moment they are saved. |
| `fi_s8_s3_hot_revoked` | A revoked object-store key aborts the backup converge cleanly rather than failing halfway through. |
| `fi_s9_resend_rate_limit` | A rate-limited transactional mail provider degrades the maintainers test bench cleanup gracefully rather than aborting it. |
| `fi_t1_incus_daemon_hang` | A hung virtualisation daemon is bounded by a timeout so the maintainers test bench cannot wedge indefinitely. |
| `fi_t3_start_sweep_failure` | A failure during the maintainers test bench start-up sweep is reported with enough context to act on rather than swallowed. |
| `fi_t4_resend_quota_zero` | An exhausted mail-provider quota makes the maintainers test bench refuse to start rather than run scenarios that cannot assert delivery. |
| `fi_u1_compose_lint_reject` | The deployment linter accepts every shipped application template and rejects a malformed one. |
| `fi_u3_gatus_baseline_down` | A service already unhealthy when an update cycle begins aborts the update rather than being read as a regression the update caused. |
| `fi_u4_ovh_rate_limited` | A rate-limited provider snapshot declines the pre-update snapshot tier without disturbing the tier below it. |
| `fi_u5_persistent_quarantine` | A version that keeps regressing stays quarantined across update cycles instead of being retried every night. |
| `fi_u6_full_rollback_state` | A rolled-back version bump is recorded in the host managed-version state, so the rollback is a fact rather than an absence. |
| `fi_v2_external_scan_blocked` | An external exposure scan blocked by the provider is downgraded to an inconclusive result rather than reported as a failure. |
| `fi_v3_tailscale_acl_misconfig` | A misconfigured private network access policy is detected up front, from both the controller and the host side. |
| `healthchecks_self_host_loss` | Losing the self-hosted monitoring instance stops its own pings without blocking the backup chain, and the external watchdog keeps the outage visible. |
| `host_reboot_recovery` | A host that is restarted comes back serving every declared service without anyone touching it. |
| `identity_group_gates_access` | Groups created in the administration panel are honoured by the access gate, and a destructive membership change cannot be applied without showing what it will affect. |
| `immich_extra_disk_round_trip` | A photo library kept on a separately mounted disk is backed up once its location is declared, and comes back whole after a rollback. |
| `infra_stack_update_rollback` | A bad version bump of an infrastructure service deployed through the application control plane is detected and rolled back automatically. |
| `infra_subdomain_change` | A shared service moves to a new address when its name is changed, and every route to it moves with it. |
| `install_without_a_domain` | A server with no domain converges green and says what it is waiting for. |
| `keycloak_admin_email_loss_recovery` | An administrator locked out of the identity provider mail channel recovers access by minting a fresh named administrator against the running server. |
| `keycloak_signing_keys_rotation_round_trip` | A single sign-on signing key is rotated with an overlap window where both keys verify, then retired without breaking any session. |
| `license_domain_mismatch` | A correctly signed licence issued for another server unlocks nothing, says so by name, and takes away nothing the host already had. |
| `mailserver_round_trip` | A real mailbox and a delivered message survive a backup and a full disaster recovery. |
| `malformed_catalog_rejection` | A malformed application catalogue aborts the run rather than being half-parsed into a silently empty one. |
| `marketplace_catalog_resolved` | The application catalogue a client browses is the one this host resolved, with every published placeholder filled in. |
| `migrate` | A host is replaced end to end by another machine, with the source quiesced and the destination serving what it served. |
| `migrate_lane_auth_denied` | The migration endpoint refuses every unauthenticated and unauthorised caller, and is absent entirely on a host that is not migrating. |
| `migrate_preseed_no_split_brain` | The bulk pre-copy leg of a migration starts nothing on the destination, so two hosts can never serve at once. |
| `migration_rollback` | A migration is called off and the source host is returned to the snapshot taken before it started. |
| `mirror_skips_on_bad_verify_hot` | The offsite copy refuses to run when the backup it would copy failed verification. |
| `mixed_template_negative_restore` | A restore whose verification fails names the application that failed, rather than reporting a generic error or a silent partial success. |
| `nc_s3_hot_recovery` | File-sync content living in object storage is recovered from the primary bucket after the live copy is lost. |
| `nc_sync_wipe_restore` | A synced folder deleted from a client device and propagated to the server is brought back from the previous day backup. |
| `nextcloud_versions_retention_applied` | The file-version retention configured for the file-sync application reaches the running container instead of stopping at the catalogue. |
| `oauth2_proxy_cookie_rotation_round_trip` | Rotating the session-cookie secret invalidates existing sessions cleanly while a fresh sign-in keeps working. |
| `offsite_copy_unreachable_target` | One offsite destination being unreachable costs exactly that copy, visibly, and leaves the others alone. |
| `panel_renames_itself` | The dashboard is renamed from the dashboard, and it says where it is going before it goes. |
| `payload_action_dispatches_without_converge` | An administrative action can arrive with a new image and dispatch immediately, without waiting for a converge to render it. |
| `payload_prune_respects_ce` | Installing the licensed payload deletes what it withdraws and leaves everything owned by the public installer in place. |
| `pg_major_version_cross_restore` | A snapshot captured on one major database version replays cleanly onto a host running a newer one. |
| `pitr_fuse_round_trip` | A single application is recovered to a point in time by browsing the backup repository as a file system. |
| `primary_domain_change_round_trip` | A server is moved to a different domain, and the domain it left stops answering. |
| `quiesce_resume_round_trip` | A host is frozen in both available degrees and resumed exactly as it was found. |
| `rclone_copy_preserves_pruned_packs` | The offsite copy keeps snapshots that have already been pruned from the primary repository. |
| `reboot_required_notified` | A host that needs a reboot reports it and waits for a person rather than restarting itself. |
| `recover_secrets_from_running_host` | A client whose installation inputs are lost rebuilds a usable credential set by reading the configuration store off the running host. |
| `recovery_landing_page_bilingual_parity` | The recovery page a client lands on presents the same content in both supported languages. |
| `recovery_readme_manual_restore` | A client rebuilds their data from a snapshot using only ordinary tools and the instructions shipped beside it. |
| `release_manifest_converge_state` | The release manifest records what the last converge delivered, so a feature that is off is distinguishable from one still waiting for an apply. |
| `repair_broken_template_round_trip` | An application whose deployment file has been hand-edited into an invalid state is repaired by re-rendering it from the canonical published source. |
| `restic_check_subset_weekly` | The backup integrity check runs its cheap structural pass and its expensive data-reading pass on separate cadences. |
| `restic_password_rotation_round_trip` | The backup repository password is rotated with an overlap where both keys open it, and the backup chain stays unbroken across the change. |
| `restore_dr` | A client whose primary machine died gets every service back on a new one, at the state the snapshot captured. |
| `restore_version_skew_abort` | A restore from a snapshot newer than the host refuses and changes nothing. |
| `restore_version_skew_upgrade` | A restore from a snapshot older than the host is refused by default and permitted only when the caller asks for the upgrade explicitly. |
| `rotate_all_secrets_sequencer` | Every secret a host holds is rotated in dependency order by one resumable sequence. |
| `s3_backup_keys_rotation_round_trip` | The object-store credentials behind both the primary and the offsite backup buckets are rotated together without breaking either. |
| `s3_reconcile_orphan_cleanup` | A database row whose object-store file is missing is reported rather than passed over in silence. |
| `scheduler_easyappointments` | The default appointment scheduler deploys from the catalogue and serves its public booking page. |
| `security_scan` | A converged host is scanned from outside for exposed services and common web vulnerabilities, and the findings are held to the declared baseline. |
| `selective_restore_round_trip` | A restore returns the data without ever taking the server out of service. |
| `smtp_rotation_round_trip` | Outbound mail credentials are rotated on the planned cycle and a live send confirms the new ones work. |
| `snapshot_export_round_trip` | A snapshot is exported to a single portable archive that unpacks away from the host with its content intact. |
| `sovereign_exit` | The suite keeps running after the administration panel is deleted, because the panel is glue rather than a data hub. |
| `swarm_overlay_selfheal` | Applications rejoin their private overlay network by themselves after the container daemon restarts. |
| `tailscale_oauth_rotation_round_trip` | The private network credential is rotated on the planned cycle and every host stays reachable across the change. |
| `unlicensed_schedules_nothing` | A host with no licence schedules no unattended work of any kind. |
| `user_recovery_2fa_reset` | A user who lost their second-factor device is reset and made to enrol a new one at next sign-in. |
| `user_recovery_kcadm_temp_password` | A user who lost their password is issued a temporary one they must change at next sign-in. |
| `verify_hot_bootprobe_weekly` | The weekly deep backup verification boots what it restored and records the outcome in its report. |
| `wizard_migrate_resume_source` | A migration that fails after the source has been stopped brings the source back into service rather than stranding the client between two machines. |
| `wizard_migrate_round_trip` | A client moves their server to a new machine end to end from the panel, with the old one still serving until the switch. |
| `wizard_restore_smoke` | A client runs a restore from the panel and gets their data back without a command line. |
| `worm_object_lock_expiry_edge` | Backup data whose immutability window expired while the repository still references it is reported as a gap rather than silently degrading. |
| `worm_round_trip` | A full recovery is performed from the immutable offsite copy alone. |
