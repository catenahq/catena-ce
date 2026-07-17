package settings

import (
	"encoding/base64"
	"encoding/json"
	"reflect"
	"testing"
)

func TestParseStoreBlankIsEmpty(t *testing.T) {
	s, err := ParseStore([]byte("   "))
	if err != nil {
		t.Fatalf("blank: %v", err)
	}
	if len(s.Secrets) != 0 || len(s.Config) != 0 {
		t.Fatalf("blank store not empty: %+v", s)
	}
}

func TestParseStoreValid(t *testing.T) {
	s, err := ParseStore([]byte(`{"secrets":{"vault_cloudflare_api_token":"cf"},"config":{"BACKUP_RESTIC_REPO":"s3:x/y"}}`))
	if err != nil {
		t.Fatalf("valid: %v", err)
	}
	if s.Secrets["vault_cloudflare_api_token"] != "cf" {
		t.Fatalf("secret not parsed: %+v", s.Secrets)
	}
	if s.Config["BACKUP_RESTIC_REPO"] != "s3:x/y" {
		t.Fatalf("config not parsed: %+v", s.Config)
	}
}

func TestParseStoreMalformed(t *testing.T) {
	if _, err := ParseStore([]byte("not json")); err == nil {
		t.Fatal("expected error on malformed store")
	}
}

func TestRedactedViewNeverEchoesSecret(t *testing.T) {
	s, _ := ParseStore([]byte(`{"secrets":{"vault_cloudflare_api_token":"cf-secret"},"config":{"NTFY_SERVER":"https://ntfy.sh"}}`))
	for _, row := range s.RedactedView() {
		if row.Kind == KindSecret && row.Value != "" {
			t.Fatalf("secret %q leaked value %q into the view", row.Key, row.Value)
		}
		if row.Key == "vault_cloudflare_api_token" && !row.Set {
			t.Fatal("cloudflare token should report Set=true")
		}
		if row.Key == "NTFY_SERVER" {
			if row.Value != "https://ntfy.sh" {
				t.Fatalf("config value not surfaced: %q", row.Value)
			}
			if !row.Set {
				t.Fatal("NTFY_SERVER should be Set")
			}
		}
		if row.Key == "vault_smtp_password" && row.Set {
			t.Fatal("unset optional secret should report Set=false")
		}
	}
}

// decodeWriteCmd pulls the base64 request payload out of a write command
// ("catena-config <b64>") and decodes it back to a request for assertions.
func decodeWriteCmd(t *testing.T, cmd string) request {
	t.Helper()
	const prefix = HostCommand + " "
	if len(cmd) <= len(prefix) || cmd[:len(prefix)] != prefix {
		t.Fatalf("command missing %q prefix: %q", prefix, cmd)
	}
	raw, err := base64.StdEncoding.DecodeString(cmd[len(prefix):])
	if err != nil {
		t.Fatalf("payload not base64: %v", err)
	}
	var req request
	if err := json.Unmarshal(raw, &req); err != nil {
		t.Fatalf("payload not json: %v", err)
	}
	return req
}

func TestBuildWriteCommandRejectsUnknownKey(t *testing.T) {
	// internal secret must not be writable through the settings form
	if _, err := BuildWriteCommand(map[string]string{"vault_admin_password": "x"}); err == nil {
		t.Fatal("expected rejection of internal-secret key")
	}
	if _, err := BuildWriteCommand(map[string]string{"nope": "x"}); err == nil {
		t.Fatal("expected rejection of unknown key")
	}
}

func TestBuildWriteCommandSkipsBlanksAndRoutesSections(t *testing.T) {
	cmd, err := BuildWriteCommand(map[string]string{
		"vault_cloudflare_api_token": "cf",
		"BACKUP_RESTIC_REPO":         "s3:x/y",
		"vault_smtp_password":        "   ", // blank -> skipped
	})
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	req := decodeWriteCmd(t, cmd)
	if req.Op != "write" {
		t.Fatalf("op = %q, want write", req.Op)
	}
	if req.Secrets["vault_cloudflare_api_token"] != "cf" {
		t.Fatalf("secret not routed: %+v", req.Secrets)
	}
	if req.Config["BACKUP_RESTIC_REPO"] != "s3:x/y" {
		t.Fatalf("config not routed: %+v", req.Config)
	}
	if _, ok := req.Secrets["vault_smtp_password"]; ok {
		t.Fatal("blank value should be skipped")
	}
}

func TestBuildWriteCommandEmptyIsNoOp(t *testing.T) {
	cmd, err := BuildWriteCommand(map[string]string{"vault_smtp_password": ""})
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	if cmd != "" {
		t.Fatalf("expected empty command for no-op submission, got %q", cmd)
	}
}

func TestBuildWriteCommandBackupConfigRouted(t *testing.T) {
	cmd, err := BuildWriteCommand(map[string]string{
		"BACKUP_ENABLED":            "true",
		"BACKUP_MIN_INTERVAL_HOURS": "168",
		"BACKUP_KEEP_DAILY":         "7",
	})
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	req := decodeWriteCmd(t, cmd)
	if req.Config["BACKUP_ENABLED"] != "true" ||
		req.Config["BACKUP_MIN_INTERVAL_HOURS"] != "168" ||
		req.Config["BACKUP_KEEP_DAILY"] != "7" {
		t.Fatalf("backup config not routed: %+v", req.Config)
	}
}

func TestBuildWriteCommandRejectsBadEnabled(t *testing.T) {
	if _, err := BuildWriteCommand(map[string]string{"BACKUP_ENABLED": "yes"}); err == nil {
		t.Fatal("expected rejection of non-boolean BACKUP_ENABLED")
	}
}

func TestBuildWriteCommandRejectsSubWeeklyInterval(t *testing.T) {
	// CE weekly-cap: sub-weekly cadence is Business.
	if _, err := BuildWriteCommand(map[string]string{"BACKUP_MIN_INTERVAL_HOURS": "24"}); err == nil {
		t.Fatal("expected rejection of sub-weekly interval on Community")
	}
	// 0 (every base fire) and >= a week are both fine.
	for _, ok := range []string{"0", "168", "336"} {
		if _, err := BuildWriteCommand(map[string]string{"BACKUP_MIN_INTERVAL_HOURS": ok}); err != nil {
			t.Fatalf("interval %q should be accepted: %v", ok, err)
		}
	}
}

func TestBuildWriteCommandRejectsBadRetention(t *testing.T) {
	for _, bad := range []string{"-1", "abc"} {
		if _, err := BuildWriteCommand(map[string]string{"BACKUP_KEEP_WEEKLY": bad}); err == nil {
			t.Fatalf("expected rejection of retention %q", bad)
		}
	}
}

func TestResticPasswordIsNotSettable(t *testing.T) {
	// USER_HELD in onbox_config: minted on-box, rotated via the dedicated
	// action -- never a settable settings field.
	if _, err := BuildWriteCommand(map[string]string{"vault_backup_restic_password": "x"}); err == nil {
		t.Fatal("restic password must be rejected by the settings write path")
	}
}

func TestReadCommandIsReadOp(t *testing.T) {
	req := decodeWriteCmd(t, ReadCommand())
	if req.Op != "read" {
		t.Fatalf("read command op = %q, want read", req.Op)
	}
}

func TestDRKeysetOrderAndOmitsUnset(t *testing.T) {
	s, _ := ParseStore([]byte(`{"secrets":{"vault_backup_s3_access_key":"ak","vault_backup_restic_password":"pw"},"config":{"BACKUP_RESTIC_REPO":"s3:x/y"}}`))
	got := s.DRKeyset()
	want := []DRKey{
		{"BACKUP_RESTIC_REPO", "s3:x/y"},
		{"vault_backup_s3_access_key", "ak"},
		// vault_backup_s3_secret_key unset -> omitted
		{"vault_backup_restic_password", "pw"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("DR keyset mismatch:\n got %v\nwant %v", got, want)
	}
}
