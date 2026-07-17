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
	"encoding/base64"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// StorePath is the on-box store the host CLI reads/writes.
const StorePath = "/etc/catena/config.json"

// HostCommand is the allow-listed host dispatch action (roles/catena-admin
// installs onbox_config.py on the host + registers this reserved action). The
// action name is a single token; the whole request rides the runner's opaque
// PAYLOAD slot as ONE base64 token, so no secret value ever meets a shell --
// the host action just base64-decodes PAYLOAD and pipes it to
// onbox_config.py --dispatch-stdin as root.
const HostCommand = "catena-config"

// request is the JSON the host dispatch decodes on stdin.
type request struct {
	Op      string            `json:"op"`
	Secrets map[string]string `json:"secrets,omitempty"`
	Config  map[string]string `json:"config,omitempty"`
}

func encodeRequest(r request) string {
	b, _ := json.Marshal(r)
	return HostCommand + " " + base64.StdEncoding.EncodeToString(b)
}

// ReadCommand is the host dispatch command that prints the full store as JSON
// on stdout. A read must never mint (viewing the settings page is read-only).
func ReadCommand() string { return encodeRequest(request{Op: "read"}) }

// HostResticValidate / HostResticRotate are the reserved host actions that
// check or re-key the backup password on the box. The restic password is
// USER_HELD (never a settable settings field): validate is read-only, rotate is
// a deliberate confirm-gated re-key, both dispatched like catena-config.
const (
	HostResticValidate = "catena-restic-validate"
	HostResticRotate   = "catena-restic-rotate"
)

type resticRequest struct {
	Op          string `json:"op"`
	Password    string `json:"password,omitempty"`
	NewPassword string `json:"new_password,omitempty"`
}

func encodeRestic(action string, r resticRequest) string {
	b, _ := json.Marshal(r)
	return action + " " + base64.StdEncoding.EncodeToString(b)
}

// BuildResticValidateCommand returns the host action that reports whether the
// supplied password opens the backup repo. Read-only.
func BuildResticValidateCommand(password string) (string, error) {
	if strings.TrimSpace(password) == "" {
		return "", fmt.Errorf("password required")
	}
	return encodeRestic(HostResticValidate, resticRequest{Op: "validate", Password: password}), nil
}

// BuildResticRotateCommand returns the host action that re-keys the repo to a
// new password. Destructive -- the old password stops opening the repo, so the
// UI must gate this behind an explicit "are you sure" confirm.
func BuildResticRotateCommand(newPassword string) (string, error) {
	if strings.TrimSpace(newPassword) == "" {
		return "", fmt.Errorf("new password required")
	}
	return encodeRestic(HostResticRotate, resticRequest{Op: "rotate", NewPassword: newPassword}), nil
}

// BuildWriteCommand validates the submitted form against the schema and returns
// the host dispatch command that persists the external creds + config. Blank
// values are skipped (never clear a stored secret); an unknown or internal key
// is rejected so a forged field cannot reach an internal secret. Returns ""
// (no error) when there is nothing to write.
func BuildWriteCommand(submitted map[string]string) (string, error) {
	req := request{Op: "write", Secrets: map[string]string{}, Config: map[string]string{}}
	changed := false
	for _, f := range Fields {
		val, ok := submitted[f.Key]
		if !ok || strings.TrimSpace(val) == "" {
			continue
		}
		if err := validateConfigValue(f.Key, val); err != nil {
			return "", err
		}
		if f.Section == SectionSecrets {
			req.Secrets[f.Key] = val
		} else {
			req.Config[f.Key] = val
		}
		changed = true
	}
	for key := range submitted {
		if _, ok := fieldByKey[key]; !ok {
			return "", fmt.Errorf("unknown settings field %q", key)
		}
	}
	if !changed {
		return "", nil
	}
	return encodeRequest(req), nil
}

// validateConfigValue enforces the small set of typed backup-config
// constraints on submission (pure; no I/O). Unlisted keys are unconstrained.
func validateConfigValue(key, val string) error {
	v := strings.TrimSpace(val)
	switch key {
	case "BACKUP_ENABLED":
		switch strings.ToLower(v) {
		case "true", "false":
			return nil
		}
		return fmt.Errorf("BACKUP_ENABLED must be true or false (got %q)", val)
	case "BACKUP_MIN_INTERVAL_HOURS":
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			return fmt.Errorf("BACKUP_MIN_INTERVAL_HOURS must be a non-negative integer (got %q)", val)
		}
		// CE weekly-cap: 0 (every base fire) or at least one week. Sub-weekly
		// cadence is the Business managed-backup tier.
		if n > 0 && n < CEWeeklyCapHours {
			return fmt.Errorf("BACKUP_MIN_INTERVAL_HOURS must be 0 or >= %d (weekly) on Community; sub-weekly cadence is Business", CEWeeklyCapHours)
		}
		return nil
	case "BACKUP_KEEP_HOURLY", "BACKUP_KEEP_DAILY", "BACKUP_KEEP_WEEKLY", "BACKUP_KEEP_MONTHLY":
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			return fmt.Errorf("%s must be a non-negative integer (got %q)", key, val)
		}
		return nil
	}
	return nil
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
	// Deferred-backup config: the run-backup wrapper reads these from the store
	// at runtime (no converge). BACKUP_ENABLED gates the run; unset -> on.
	// BACKUP_MIN_INTERVAL_HOURS lengthens the effective cadence beyond the base
	// weekly timer (CE weekly-cap: 0 or >= 168). Retention feeds restic forget.
	{"BACKUP_ENABLED", SectionConfig, KindText, true, "settings.field.backup_enabled"},
	{"BACKUP_MIN_INTERVAL_HOURS", SectionConfig, KindText, true, "settings.field.backup_min_interval_hours"},
	{"BACKUP_KEEP_HOURLY", SectionConfig, KindText, true, "settings.field.backup_keep_hourly"},
	{"BACKUP_KEEP_DAILY", SectionConfig, KindText, true, "settings.field.backup_keep_daily"},
	{"BACKUP_KEEP_WEEKLY", SectionConfig, KindText, true, "settings.field.backup_keep_weekly"},
	{"BACKUP_KEEP_MONTHLY", SectionConfig, KindText, true, "settings.field.backup_keep_monthly"},
	{"SMTP_HOST", SectionConfig, KindText, true, "settings.field.smtp_host"},
	{"SMTP_PORT", SectionConfig, KindText, true, "settings.field.smtp_port"},
	{"SMTP_FROM", SectionConfig, KindText, true, "settings.field.smtp_from"},
	{"NTFY_SERVER", SectionConfig, KindText, true, "settings.field.ntfy_server"},
	{"NTFY_TOPIC", SectionConfig, KindText, true, "settings.field.ntfy_topic"},
}

// CEWeeklyCapHours is the Community backup-cadence floor: CE ships a single
// weekly timer, so the runtime interval must be 0 (every base fire) or at least
// one week. Sub-weekly cadence is the Business managed-backup tier.
const CEWeeklyCapHours = 168

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
