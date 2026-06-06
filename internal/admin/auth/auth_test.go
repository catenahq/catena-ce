package auth

import (
	"net/http"
	"testing"
)

func TestParseGroups(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want []string
	}{
		{"comma", "admin,staff", []string{"admin", "staff"}},
		{"newline", "admin\nstaff", []string{"admin", "staff"}},
		{"mixed + whitespace", " admin , staff\nclient ", []string{"admin", "client", "staff"}},
		{"dedup", "staff,admin,staff", []string{"admin", "staff"}},
		{"empty", "", nil},
		{"only separators", " , \n ", nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := ParseGroups(tc.raw)
			if len(got) != len(tc.want) {
				t.Fatalf("ParseGroups(%q) = %v, want %v", tc.raw, got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Fatalf("ParseGroups(%q) = %v, want %v", tc.raw, got, tc.want)
				}
			}
		})
	}
}

func TestIdentityFromRequest(t *testing.T) {
	r, _ := http.NewRequest("GET", "/", nil)
	r.Header.Set("X-Forwarded-Email", "  marc@example.com ")
	r.Header.Set("X-Forwarded-Preferred-Username", "Marc")
	r.Header.Set("X-Forwarded-Groups", "admin,staff")
	id, err := IdentityFromRequest(r)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if id.Email != "marc@example.com" {
		t.Errorf("Email = %q, want trimmed marc@example.com", id.Email)
	}
	if id.DisplayName != "Marc" {
		t.Errorf("DisplayName = %q, want Marc", id.DisplayName)
	}
	if !id.IsAdmin() {
		t.Error("expected IsAdmin true for admin group")
	}
	if !id.IsAuthenticated() {
		t.Error("expected IsAuthenticated true")
	}
}

func TestNonAdminIsNotAdmin(t *testing.T) {
	r, _ := http.NewRequest("GET", "/", nil)
	r.Header.Set("X-Forwarded-Email", "staff@example.com")
	r.Header.Set("X-Forwarded-Groups", "staff,client")
	id, _ := IdentityFromRequest(r)
	if id.IsAdmin() {
		t.Error("staff/client must not be admin")
	}
}

func TestHeaderSigNoOpWhenNotRequired(t *testing.T) {
	t.Setenv(requireSigEnv, "")
	t.Setenv(headerSecretEnv, "")
	r, _ := http.NewRequest("GET", "/", nil)
	r.Header.Set("X-Forwarded-Email", "a@b.c")
	if _, err := IdentityFromRequest(r); err != nil {
		t.Fatalf("sig check should be a no-op when not required: %v", err)
	}
}

func TestHeaderSigValidPasses(t *testing.T) {
	t.Setenv(requireSigEnv, "1")
	t.Setenv(headerSecretEnv, "s3cret")
	r, _ := http.NewRequest("GET", "/", nil)
	r.Header.Set("X-Forwarded-Email", "a@b.c")
	r.Header.Set("X-Forwarded-Groups", "admin")
	r.Header.Set("X-Forwarded-Preferred-Username", "A")
	r.Header.Set(sigHeader, computeHeaderSig("a@b.c", "admin", "A"))
	if _, err := IdentityFromRequest(r); err != nil {
		t.Fatalf("valid sig should pass: %v", err)
	}
}

func TestHeaderSigMissingRejected(t *testing.T) {
	t.Setenv(requireSigEnv, "1")
	t.Setenv(headerSecretEnv, "s3cret")
	r, _ := http.NewRequest("GET", "/", nil)
	r.Header.Set("X-Forwarded-Email", "a@b.c")
	if _, err := IdentityFromRequest(r); err != ErrMissingSig {
		t.Fatalf("missing sig = %v, want ErrMissingSig", err)
	}
}

func TestHeaderSigMismatchRejected(t *testing.T) {
	t.Setenv(requireSigEnv, "1")
	t.Setenv(headerSecretEnv, "s3cret")
	r, _ := http.NewRequest("GET", "/", nil)
	r.Header.Set("X-Forwarded-Email", "a@b.c")
	r.Header.Set("X-Forwarded-Groups", "admin")
	r.Header.Set(sigHeader, "deadbeef")
	if _, err := IdentityFromRequest(r); err != ErrSigMismatch {
		t.Fatalf("bad sig = %v, want ErrSigMismatch", err)
	}
}

func TestHeaderSigFlagWithoutSecretIsNoOp(t *testing.T) {
	// Flag on but no secret -> fail closed to "not required" so the shell
	// does not silently always-allow with an empty-key HMAC.
	t.Setenv(requireSigEnv, "1")
	t.Setenv(headerSecretEnv, "")
	r, _ := http.NewRequest("GET", "/", nil)
	if _, err := IdentityFromRequest(r); err != nil {
		t.Fatalf("flag without secret must be a no-op, got %v", err)
	}
}
