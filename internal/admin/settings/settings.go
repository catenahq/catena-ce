// Package settings is the catena-admin on-box config surface (0b): the
// client's self-serve view of the external vendor credentials + non-secret
// config that live in the /etc/catena/config.json store on the host.
//
// The admin container runs unprivileged and mounts the host read-only, so it
// CANNOT write the root-owned 0600 store directly. Writes are dispatched to
// the host through the shared actions.Runner, which runs the onbox_config.py
// CLI as root -- the same host-dispatch machinery the Actions tab uses, and
// the seam the test bench drives to provision a host over HTTP.
//
// This package owns four things, all pure (no I/O), so they unit-test without
// a host:
//   - the editable field schema (external secrets + client-facing config),
//   - a REDACTED read view that reports which secrets are set but never
//     echoes a secret value back to the browser,
//   - the write-command argument builder (form submission -> onbox_config.py
//     args), which rejects any key outside the schema so a forged field
//     cannot smuggle an internal-secret override,
//   - the DR-keyset export view (the one place real secret values surface --
//     the restic repo + S3 keys + restic password the client must save).
package settings

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

// StorePath is the on-box store the host CLI reads/writes.
const StorePath = "/etc/catena/config.json"

// HostCommand is the host-side wrapper the admin container dispatches through
// the actions.Runner (installed + allow-listed by roles/catena-admin). It runs
// onbox_config.py as root against the store, so the unprivileged container
// never touches the root-owned 0600 file directly.
const HostCommand = "catena-config"

// ReadArgs reads the full store without minting (a settings-page GET must not
// mint secrets as a side effect of viewing).
func ReadArgs() []string {
	return []string{"--path", StorePath, "--emit", "all", "--no-mint"}
}

// ShellCommand renders HostCommand + args as one shell-safe command string for
// Runner.Run (executed as $SSH_ORIGINAL_COMMAND on the host). Every arg is
// single-quoted so a secret value with shell metacharacters cannot break out.
func ShellCommand(args []string) string {
	parts := make([]string, 0, len(args)+1)
	parts = append(parts, HostCommand)
	for _, a := range args {
		parts = append(parts, shellQuote(a))
	}
	return strings.Join(parts, " ")
}

func shellQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}

// FieldKind drives the input widget + redaction rule.
type FieldKind int

const (
	// KindSecret is never echoed back to the browser (write-only from the
	// form's perspective); the read view reports only whether it is set.
	KindSecret FieldKind = iota
	// KindText is a non-secret config value; the read view shows it.
	KindText
)

// Section is the config.json top-level bucket a field lives in.
type Section string

const (
	SectionSecrets Section = "secrets"
	SectionConfig  Section = "config"
)

// Field is one editable entry on the settings page.
type Field struct {
	Key      string    // config.json key: vault_* for secrets, ENV name for config
	Section  Section   //
	Kind     FieldKind //
	Optional bool      // an empty value is acceptable
	LabelKey string    // i18n message key
}

// IsSecret reports whether the field is a write-only secret (template helper).
func (f Field) IsSecret() bool { return f.Kind == KindSecret }

// IsText reports whether the field is a visible config value (template helper).
func (f Field) IsText() bool { return f.Kind == KindText }

// Fields is the client-facing editable schema: the external vendor
// credentials the client supplies + the non-secret backup/mail config. It
// deliberately omits every internal generated secret (those mint on-box and
// are never client-edited) and omits vault_portainer_api_key (Portainer mints
// it). Keep the secret keys a subset of onbox_config.py EXTERNAL_SECRETS.
var Fields = []Field{
	// --- external vendor credentials (secrets) ---
	{"vault_cloudflare_api_token", SectionSecrets, KindSecret, false, "settings.field.cloudflare_api_token"},
	{"vault_tailscale_oauth_client_id", SectionSecrets, KindSecret, false, "settings.field.tailscale_oauth_client_id"},
	{"vault_tailscale_oauth_client_secret", SectionSecrets, KindSecret, false, "settings.field.tailscale_oauth_client_secret"},
	{"vault_backup_s3_access_key", SectionSecrets, KindSecret, false, "settings.field.backup_s3_access_key"},
	{"vault_backup_s3_secret_key", SectionSecrets, KindSecret, false, "settings.field.backup_s3_secret_key"},
	{"vault_smtp_password", SectionSecrets, KindSecret, true, "settings.field.smtp_password"},
	{"vault_mailserver_relay_password", SectionSecrets, KindSecret, true, "settings.field.mailserver_relay_password"},
	{"vault_mailserver_spamhaus_dqs_key", SectionSecrets, KindSecret, true, "settings.field.mailserver_spamhaus_dqs_key"},
	{"vault_nextcloud_s3_access_key", SectionSecrets, KindSecret, true, "settings.field.nextcloud_s3_access_key"},
	{"vault_nextcloud_s3_secret_key", SectionSecrets, KindSecret, true, "settings.field.nextcloud_s3_secret_key"},
	// --- non-secret config ---
	{"BACKUP_RESTIC_REPO", SectionConfig, KindText, false, "settings.field.backup_restic_repo"},
	{"SMTP_HOST", SectionConfig, KindText, true, "settings.field.smtp_host"},
	{"SMTP_PORT", SectionConfig, KindText, true, "settings.field.smtp_port"},
	{"SMTP_FROM", SectionConfig, KindText, true, "settings.field.smtp_from"},
	{"NTFY_SERVER", SectionConfig, KindText, true, "settings.field.ntfy_server"},
	{"NTFY_TOPIC", SectionConfig, KindText, true, "settings.field.ntfy_topic"},
}

// fieldByKey indexes Fields for O(1) schema validation.
var fieldByKey = func() map[string]Field {
	m := make(map[string]Field, len(Fields))
	for _, f := range Fields {
		m[f.Key] = f
	}
	return m
}()

// Store is the parsed on-box config store.
type Store struct {
	Secrets map[string]string `json:"secrets"`
	Config  map[string]string `json:"config"`
}

// ParseStore decodes the JSON the host CLI emits (onbox_config.py --emit all).
// A blank input is an empty store (the pre-bootstrap state), not an error.
func ParseStore(raw []byte) (Store, error) {
	s := Store{Secrets: map[string]string{}, Config: map[string]string{}}
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" {
		return s, nil
	}
	if err := json.Unmarshal([]byte(trimmed), &s); err != nil {
		return Store{}, fmt.Errorf("parse store: %w", err)
	}
	if s.Secrets == nil {
		s.Secrets = map[string]string{}
	}
	if s.Config == nil {
		s.Config = map[string]string{}
	}
	return s, nil
}

// FieldStatus is one row of the redacted read view.
type FieldStatus struct {
	Field
	// Set reports whether the store holds a non-empty value for this key.
	Set bool
	// Value is the CURRENT value for KindText config only; always "" for
	// KindSecret so a secret is never serialized into the settings page.
	Value string
}

// RedactedView reports, per field, whether it is set and (config only) its
// current value. Secret values are never included.
func (s Store) RedactedView() []FieldStatus {
	out := make([]FieldStatus, 0, len(Fields))
	for _, f := range Fields {
		var cur string
		switch f.Section {
		case SectionSecrets:
			cur = s.Secrets[f.Key]
		case SectionConfig:
			cur = s.Config[f.Key]
		}
		st := FieldStatus{Field: f, Set: strings.TrimSpace(cur) != ""}
		if f.Kind == KindText {
			st.Value = cur
		}
		out = append(out, st)
	}
	return out
}

// BuildWriteArgs turns a submitted {key: value} form into the onbox_config.py
// argument list. Blank values are skipped (never clear an existing secret).
// Any key outside the schema is rejected -- a forged/renamed field cannot
// reach an internal secret or an arbitrary config key. Secrets and config use
// --set-secret / --set-config respectively; --overwrite makes a submitted
// value replace the stored one (the client is entering a correction). The args
// are deterministic (schema order) for stable tests + logs.
func BuildWriteArgs(submitted map[string]string) ([]string, error) {
	for key := range submitted {
		if _, ok := fieldByKey[key]; !ok {
			return nil, fmt.Errorf("unknown settings field %q", key)
		}
	}
	args := []string{"--path", StorePath, "--overwrite", "--emit", "none"}
	changed := false
	for _, f := range Fields {
		val, ok := submitted[f.Key]
		if !ok || strings.TrimSpace(val) == "" {
			continue
		}
		flag := "--set-secret"
		if f.Section == SectionConfig {
			flag = "--set-config"
		}
		args = append(args, flag, f.Key+"="+val)
		changed = true
	}
	if !changed {
		return nil, nil // nothing to write
	}
	return args, nil
}

// DRKey is one row of the disaster-recovery export: the values the client must
// save to rebuild from backup. This is the ONE view that surfaces real secret
// values, shown once so the client can copy them to their password manager.
type DRKey struct {
	Key   string
	Value string
}

// DRKeyOrder is the disaster-recovery keyset (restic repo URL + S3 creds +
// restic password): with these three the client can restore a wiped VPS.
var drKeyOrder = []struct {
	key     string
	section Section
}{
	{"BACKUP_RESTIC_REPO", SectionConfig},
	{"vault_backup_s3_access_key", SectionSecrets},
	{"vault_backup_s3_secret_key", SectionSecrets},
	{"vault_backup_restic_password", SectionSecrets},
}

// DRKeyset returns the disaster-recovery keyset values from the store, in a
// stable order, omitting any that are not yet set.
func (s Store) DRKeyset() []DRKey {
	out := make([]DRKey, 0, len(drKeyOrder))
	for _, d := range drKeyOrder {
		var v string
		switch d.section {
		case SectionSecrets:
			v = s.Secrets[d.key]
		case SectionConfig:
			v = s.Config[d.key]
		}
		if strings.TrimSpace(v) == "" {
			continue
		}
		out = append(out, DRKey{Key: d.key, Value: v})
	}
	return out
}

// SecretKeys returns the schema's secret keys (sorted) -- used by tests and by
// the caller that cross-checks the schema against onbox_config EXTERNAL_SECRETS.
func SecretKeys() []string {
	var out []string
	for _, f := range Fields {
		if f.Section == SectionSecrets {
			out = append(out, f.Key)
		}
	}
	sort.Strings(out)
	return out
}
