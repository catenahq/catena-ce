package settings

import (
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

func TestBuildWriteArgsRejectsUnknownKey(t *testing.T) {
	// internal secret must not be writable through the settings form
	if _, err := BuildWriteArgs(map[string]string{"vault_admin_password": "x"}); err == nil {
		t.Fatal("expected rejection of internal-secret key")
	}
	if _, err := BuildWriteArgs(map[string]string{"nope": "x"}); err == nil {
		t.Fatal("expected rejection of unknown key")
	}
}

func TestBuildWriteArgsSkipsBlanksAndRoutesSections(t *testing.T) {
	args, err := BuildWriteArgs(map[string]string{
		"vault_cloudflare_api_token": "cf",
		"BACKUP_RESTIC_REPO":         "s3:x/y",
		"vault_smtp_password":        "   ", // blank -> skipped
	})
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	want := []string{
		"--path", StorePath, "--overwrite", "--emit", "none",
		"--set-secret", "vault_cloudflare_api_token=cf",
		"--set-config", "BACKUP_RESTIC_REPO=s3:x/y",
	}
	if !reflect.DeepEqual(args, want) {
		t.Fatalf("args mismatch:\n got %v\nwant %v", args, want)
	}
}

func TestBuildWriteArgsEmptyIsNil(t *testing.T) {
	args, err := BuildWriteArgs(map[string]string{"vault_smtp_password": ""})
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	if args != nil {
		t.Fatalf("expected nil args for no-op submission, got %v", args)
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
