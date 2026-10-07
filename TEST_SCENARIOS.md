<!-- AUTOGEN by `audit --write-public-specs` from the test bench's
     scenario headers. Do not hand-edit; the maintainers' CI
     regenerates and fails on any drift
     (`audit --check-public-specs`). -->

# Test scenarios

The 165 scenarios the maintainers' test bench carries, and the behaviour each one is written to prove against a real virtual machine. Names are stable: a SPEC.md invariant citing `bench:<name>` refers to the row of the same name below.

| Scenario | What it proves |
| --- | --- |
| `activate_ee` | A Community host becomes a licensed host when a key Polar grants is saved in its Settings, and unlocks nothing for a key Polar refuses. |
| `admin_action_unknown_rejected` | The administrative action dispatcher refuses a request for an action it does not carry, exits non-zero, and names nothing it might have run. |
| `audit_chain_tamper_evident` | An exported administrative audit trail verifies away from the host it came from, and stops verifying as soon as any row is altered. |
| `backup_rollback` | A file changed after a backup is returned to its snapshot content by a rollback, and the applications come back with it. |
| `backup_schedule_applied` | The scheduled maintenance a host actually runs matches what its configuration store asks for, and a freshly converged host schedules nothing at all. |
| `catena_admin_self_update` | The administration panel updates itself as one unit -- the container, the engines it installs on the host and the host configuration the new version carries -- and restores all three if the new version is bad. |
| `ce_admin_actions` | Pressing a maintenance button in the Community administration panel dispatches the action and streams its result back. |
| `ce_admin_smoke` | A Community host carries the host-side state its administration panel needs, and the panel answers. |
| `ce_backup_deferred` | A host with no scheduled backup can still take one on demand, because whether a host backs up is answered by its storage credentials rather than by a switch. |
| `ce_install_headscale` | A host whose panel is given a self-hosted private network control server joins that server instead of the hosted one. |
| `ce_install_suite` | A Community install, configured in its panel, brings up the full application suite rather than the base host alone. |
| `ce_restore` | A Community host restores from its encrypted backup and comes back with the content the snapshot held. |
| `ce_uninstall` | Uninstalling hands the host back to the operating system, including the package-update timers the install had taken over. |
| `ce_validate` | A converged Community host validates itself end to end using only the shipped installer. |
| `cf_tunnel_regenerate_round_trip` | Rotating the public tunnel mints a new one, repoints the public name at it, revokes the old one, and rolls the connector onto the new credential. |
| `clamd_reload_on_a_small_host` | On a machine below the 8 GB a supported server has, a signature reload of the shared antivirus scanner keeps memory flat, and mail that arrives during the reload is still delivered. |
| `cloudflare_api_rotation_round_trip` | The edge-provider API credential is rotated on the planned cycle and the host keeps serving across the change. |
| `concurrent_backup_lock_contention` | Two maintenance jobs contending for the host lock run one after the other instead of at the same time. |
| `container_delete_recreated` | A container deleted out from under the orchestrator, and a whole service deleted with it, are scheduled again and come back serving. |
| `control_plane_update_rollback` | A bad version bump of a control-plane service is detected, rolled back automatically, and quarantined so it is never retried blindly. |
| `converge_suite` | One converge after an on-host image bump, a doctored release record and a rotated secret keeps the bump, restores the record and delivers the secret. |
| `cve_residual_emits_findings` | The vulnerability scan produces a well-formed machine-readable report on a real host, whether or not it finds anything. |
| `daily_chain_container_rollback` | A failed container update inside the daily maintenance chain rolls that service back and lets the chain continue. |
| `daily_chain_full_pass` | The daily maintenance chain runs every stage end to end on a real host and reports itself idle when it finishes. |
| `daily_chain_preflight_aborts_low_disk` | The daily maintenance chain refuses to start when free disk at the staging area is below the configured floor. |
| `daily_chain_quiesce_invoked` | The daily maintenance chain quiesces applications before taking a backup and always releases them afterwards, including when the backup aborts the chain. |
| `daily_chain_reboot_round_trip` | The nightly maintenance restarts a host whose installed updates wait for a restart, and checks that everything running before it came back. |
| `daily_chain_resumes_after_reboot` | A daily maintenance chain cut short by a reboot resumes when the host comes back, on a licensed host whose daily schedule is on. |
| `daily_chain_updates_without_backup` | Container updates still run on a server with no backup configured, behind the same health gate, and the maintenance log says no backup stands behind them. |
| `daily_chain_verify_cold_blocks_mirror` | A failed cold-storage verification stops the offsite copy from pushing, so a bad archive is never propagated. |
| `daily_chain_verify_cold_fail_configurable` | Whether a failed cold-storage verification blocks the update tail of the daily chain follows the host configuration rather than being hard- wired. |
| `daily_chain_verify_hot_fail_aborts_updates` | A failed backup verification aborts the update tail of the daily chain, preserving a known-good rollback target. |
| `daily_state_corrupt_fallback` | A corrupted daily maintenance state file fails the chain with a readable diagnostic, and the reset command returns the host to the start of the chain. |
| `daily_umbrella_healthchecks` | The daily maintenance chain signals an external monitor when it starts and again when it succeeds. |
| `debian_major_upgrade_restore` | A snapshot taken on one major operating-system release restores onto a host running the next one. |
| `dev_to_prod_cutover_round_trip` | A staging deployment is promoted to its production hostname on the same machine, with every application reconciled to the new address. |
| `docker_engine_upgrade_round_trip` | The container engine moves to the version the panel image pins, and goes back to the one it ran when the new one does not come up. |
| `dr_suite` | A lost server is rebuilt on a new machine from one backup, with every service, the stored mail and the synced files back at the state that backup captured, including a file deleted after the backup. |
| `ee_attest` | A signed monthly compliance attestation is produced from a host's own evidence and verifies against the published key. |
| `ee_audit_ship` | Administrative audit events shipped from a host arrive intact at the central collector. |
| `ee_ce_regression` | Community actions keep working on a host that has been licensed for Pro. |
| `ee_daily_cycle` | The daily maintenance engine drives a full chain on a licensed host, stage by stage, to completion. |
| `ee_entitlement_partial` | A Catena Pro key unlocks the Pro panels and nothing of Business, and the actions behind the Business panels are absent rather than merely hidden. |
| `ee_identity_probe` | The identity posture probe reports how single sign-on is configured on a host and changes verdict when that configuration changes. |
| `ee_lapse` | A host whose key Polar stops granting locks its licensed features once, tells its admin once, and keeps everything Community working. |
| `ee_multidomain` | A second, unrelated domain is attached to a licensed host, both domains are served through the one tunnel, and a gated app on each domain sends its visitors to its own domain's sign-on, with cookies scoped to that domain. |
| `ee_named_buttons` | Each licensed maintenance button dispatches its own distinct action rather than sharing one. |
| `erpnext_failed_migrate_rolls_back` | When an ERPNext update's database migration fails, the previous version and its database are put back and the site keeps serving. |
| `erpnext_update_migrates` | An ERPNext update moves every ERPNext service to the new version in one step and migrates the database, and the site comes back serving. |
| `fi_a2_oidc_secret_rotation` | Rotating the single sign-on client secret propagates to both the identity provider and the proxy in one converge. |
| `fi_a3_keycloak_unreachable` | An unreachable identity provider is reported as a failed readiness gate rather than passing silently. |
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
| `fi_c7_coturn_cert_expired` | A media-relay certificate that is due is renewed by the scheduled renewal job, and the relay reloads it. |
| `fi_c8_nextcloud_init_loop` | An application whose environment has been corrupted fails visibly with captured diagnostics rather than looping in silence. |
| `fi_d2_pg_dumpall_replay_constraint` | Replaying a database dump recreates a database role the cluster has lost. |
| `fi_d3_postgres_oom_mid_restore` | A restore that would exhaust the host disk is refused before it starts, rather than leaving a half-loaded database. |
| `fi_d4_disk_full_mid_snapshot` | A backup that runs out of disk partway through fails loudly and writes no half-finished archive. |
| `fi_d5_disk_full_mid_converge` | A converge on a host with too little free disk aborts at its preflight check before pulling anything. |
| `fi_d6_volume_uid_drift` | A service whose user has drifted away from what it requires is reconciled back by the next converge. |
| `fi_d7_restic_corrupt_pack` | Corruption introduced into the backup repository is detected by the deep verification pass. |
| `fi_n10_multidomain_cap` | A Community host asked to serve more than one domain degrades to the one it supports rather than refusing to converge. |
| `fi_n11_lockdown_refuses_shields_up` | A lockdown applied from the panel refuses to close public SSH while the private network reports the server as refusing incoming connections. |
| `fi_n1_tailnet_partition_mid_converge` | A private network partition fails the lockdown's reachability proof instead of proceeding blind. |
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
| `fi_s4_tailscale_oauth_revoked` | A revoked private network credential entered in the panel is refused by the check its save runs on the host, and never stored. |
| `fi_s7_smtp_creds_invalid` | Invalid outbound mail credentials surface as an authentication failure from a live probe at the moment they are saved. |
| `fi_s8_s3_hot_revoked` | A revoked object-store key aborts the backup converge cleanly rather than failing halfway through. |
| `fi_s9_resend_rate_limit` | A rate-limited transactional mail provider degrades the maintainers test bench cleanup gracefully rather than aborting it. |
| `fi_t3_start_sweep_failure` | A failure during the maintainers test bench start-up sweep is reported with enough context to act on rather than swallowed. |
| `fi_t4_resend_quota_zero` | An exhausted mail-provider quota makes the maintainers test bench refuse to start rather than run scenarios that cannot assert delivery. |
| `fi_u1_compose_lint_reject` | The template linter accepts every shipped application template and rejects a malformed one. |
| `fi_u3_gatus_baseline_down` | A service already unhealthy when an update cycle begins aborts the update rather than being read as a regression the update caused. |
| `fi_u4_ovh_rate_limited` | A rate-limited provider snapshot declines the pre-update snapshot tier without disturbing the tier below it. |
| `fi_u5_persistent_quarantine` | A version that regressed stays quarantined across update cycles and is recorded once. |
| `fi_u6_full_rollback_state` | A rolled-back version bump is recorded in the host managed-version state, so the rollback is a fact rather than an absence. |
| `fi_v2_external_scan_blocked` | An external exposure scan blocked by the provider is downgraded to an inconclusive result rather than reported as a failure. |
| `fi_v3_tailscale_acl_misconfig` | A private network access policy that blocks SSH to the server stops the lockdown before public SSH closes. |
| `healthchecks_self_host_loss` | Losing the self-hosted monitoring instance stops its own pings without blocking the backup chain, and the external watchdog keeps the outage visible. |
| `host_reboot_recovery` | A host that is restarted comes back serving every declared service without anyone touching it. |
| `identity_group_gates_access` | Groups created in the administration panel are honoured by the access gate, and a destructive membership change cannot be applied without showing what it will affect. |
| `immich_extra_disk_round_trip` | A photo library kept on a separately mounted disk is backed up once its location is declared, and comes back whole after a rollback. |
| `infra_stack_update_rollback` | A bad version bump of an infrastructure service deployed through the application control plane is detected and rolled back automatically. |
| `infra_subdomain_change` | A shared service moves to a new address when its name is changed, and every route to it moves with it. |
| `inplace_restore_suite` | A restore on a running server brings the data back without taking the dashboard down, can put back one application while leaving the others alone, can be started from the dashboard, and refuses a backup from a newer installer, or from an older one unless the upgrade is asked for, before it changes anything. |
| `install_rerun` | Applying the Community installer a second time to an already converged host changes nothing. |
| `install_without_a_domain` | A server with no domain converges green and says what it is waiting for. |
| `keycloak_admin_email_loss_recovery` | An administrator locked out of the identity provider mail channel recovers access by minting a fresh named administrator against the running server. |
| `keycloak_signing_keys_rotation_round_trip` | A single sign-on signing key is rotated with an overlap window where both keys verify, then retired without breaking any session. |
| `labels_vocabulary_from_image` | The port a template's labels declare is read with the label vocabulary the server's panel image ships, so an image update changes what a label means with no converge. |
| `launcher_install` | The graphical installer, run with no UI, installs a server the way the command-line installer does, over a private address and over the public one. |
| `license_seat` | A key is active on one server at a time: a copy of a licensed server and a second server are both refused the key's seat, and once the seat is freed the second server takes it and the first locks at its next check -- while the first keeps everything but its paid panels. |
| `mailbox_sync_ownership` | A mailbox made by hand survives the mailbox reconciler, and a mailbox the reconciler made is closed when its owner leaves the staff group. |
| `mailserver_after_domain` | A mail server deployed from the catalogue on a server that already has its domain is wired with nothing pressed: the server starts the converge that wires it, once. |
| `malformed_catalog_rejection` | A malformed application catalogue is refused, and the host keeps using the last one it read correctly. |
| `marketplace_catalog_resolved` | The application catalogue a client browses is the one this host resolved, with every published placeholder filled in. |
| `migrate` | A host is replaced end to end by another machine, with the source quiesced and the destination serving what it served. |
| `migrate_lane_auth_denied` | The migration endpoint refuses every unauthenticated and unauthorised caller, and is absent entirely on a host that is not migrating. |
| `migrate_preseed_no_split_brain` | The bulk pre-copy leg of a migration starts nothing on the destination, so two hosts can never serve at once. |
| `mirror_skips_on_bad_verify_hot` | The offsite copy refuses to run when the backup it would copy failed verification. |
| `mixed_template_negative_restore` | A restore whose verification fails names the application that failed, rather than reporting a generic error or a silent partial success. |
| `nextcloud_versions_retention_applied` | The file-version retention configured for the file-sync application reaches the running container instead of stopping at the catalogue. |
| `oauth2_proxy_cookie_rotation_round_trip` | Rotating the session-cookie secret invalidates existing sessions cleanly while a fresh sign-in keeps working. |
| `offsite_copy_unreachable_target` | One offsite destination being unreachable costs exactly that copy, visibly, and leaves the others alone. |
| `panel_release_moves_pins` | A new administration panel release moves the reverse proxy and the uptime monitor to the versions it ships, on a Community server, through the panel's update button alone. |
| `panel_renames_itself` | The dashboard renames itself from its own Settings page, and it says where it is going before it goes. |
| `payload_action_dispatches_without_converge` | An administrative action can arrive with a new image and dispatch immediately, without waiting for a converge to render it. |
| `payload_prune_respects_ce` | Installing the licensed payload deletes what it withdraws and leaves everything owned by the public installer in place. |
| `pg_major_version_cross_restore` | A snapshot captured on one major database version replays cleanly onto a host running a newer one. |
| `pitr_fuse_round_trip` | A single application is recovered to a point in time by browsing the backup repository as a file system. |
| `polar_api_version_current` | The Polar API version the licence check pins still answers in Polar's sandbox, and a newer version Polar publishes is reported as a warning to move the pin before the pinned one is removed. |
| `polar_sandbox_smoke` | Polar's sandbox grants a licence key to one server's hardware, confirms it there, and refuses the same key to a second server while the first holds its only seat. |
| `polar_unreachable_grace` | A licensed host keeps its edition while Polar cannot be reached for up to 48 hours, locks past that and tells its admin once, and unlocks as soon as Polar answers again. |
| `post_compromise_rebuild` | A server rebuilt after a compromise comes back on a new object-store key pair, with the pair its backups were taken under revoked, and stays off the public edge until its Cloudflare token is replaced. |
| `primary_domain_change_round_trip` | A server is moved to a different domain, and the domain it left stops answering. |
| `quiesce_resume_round_trip` | A host is frozen in both available degrees and resumed exactly as it was found. |
| `rclone_copy_preserves_pruned_packs` | The offsite copy keeps snapshots that have already been pruned from the primary repository. |
| `reboot_required_notified` | A host that needs a reboot reports it and waits for a person rather than restarting itself. |
| `rebuild_backs_up_after_restore` | A server rebuilt from its backup keeps backing up: after the restore it holds the repository's password, not the one the new machine made for itself. |
| `reconcile_is_a_noop` | The operator's converge and the host's own converge produce the same host: after either one, the other changes nothing, and a second converge in a row restarts nothing. |
| `reconcile_repairs_host_upkeep` | A host that converges itself puts back the upkeep it owns: unattended upgrades, the journal's storage, the mail-agent mask, the swarm's settings and the configuration its scheduled lanes read. |
| `recover_secrets_from_running_host` | A client whose installation inputs are lost rebuilds a usable credential set by reading the configuration store off the running host. |
| `recovery_landing_page_bilingual_parity` | The recovery page a client lands on presents the same content in both supported languages. |
| `recovery_readme_manual_restore` | A client rebuilds their data from a snapshot using only ordinary tools and the instructions shipped beside it. |
| `repair_broken_template_round_trip` | An application whose deployment file has been hand-edited into an invalid state is repaired by re-rendering it from the canonical published source. |
| `restic_check_subset_weekly` | The backup integrity check runs its cheap structural pass and its expensive data-reading pass on separate cadences. |
| `restic_password_rotation_round_trip` | The backup repository password is rotated with an overlap where both keys open it, and the backup chain stays unbroken across the change. |
| `rotate_all_secrets_sequencer` | Every secret a host holds is rotated in dependency order by one resumable sequence. |
| `s3_backup_keys_rotation_round_trip` | The object-store credentials behind both the primary and the offsite backup buckets are rotated together without breaking either. |
| `s3_reconcile_orphan_cleanup` | A database row whose object-store file is missing is reported rather than passed over in silence. |
| `scheduler_easyappointments` | The default appointment scheduler deploys from the catalogue and serves its public booking page. |
| `security_scan` | A converged host is scanned from outside for exposed services and common web vulnerabilities, and the findings are held to the declared baseline. |
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
| `worm_object_lock_expiry_edge` | Backup data whose immutability window expired while the repository still references it is reported as a gap rather than silently degrading. |
| `worm_round_trip` | A full recovery is performed from the immutable offsite copy alone. |
