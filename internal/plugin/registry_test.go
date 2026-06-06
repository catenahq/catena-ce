package plugin

import (
	"crypto/ed25519"
	"crypto/rand"
	"testing"
	"time"

	"github.com/catenahq/catena/internal/license"
)

type stubPlugin struct {
	id      string
	edition license.Edition
}

func (s stubPlugin) ID() string               { return s.id }
func (s stubPlugin) Title() string            { return s.id }
func (s stubPlugin) Edition() license.Edition { return s.edition }

func activeBusinessLicense(t *testing.T, now time.Time) *license.License {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("keygen: %v", err)
	}
	tok, err := license.Sign(license.Claims{
		Subject:    "acme",
		Edition:    license.Business,
		IssuedAt:   now.Add(-time.Hour),
		ValidUntil: now.Add(24 * time.Hour),
	}, priv)
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	lic, err := license.Verify(tok, pub)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	return lic
}

func ids(ps []Plugin) []string {
	out := make([]string, len(ps))
	for i, p := range ps {
		out[i] = p.ID()
	}
	return out
}

func newTestRegistry() *Registry {
	r := NewRegistry()
	r.Register(stubPlugin{id: "actions", edition: license.Community})
	r.Register(stubPlugin{id: "audit", edition: license.Business})
	r.Register(stubPlugin{id: "access", edition: license.Business})
	return r
}

func TestEnabledCEOnlyWithoutLicense(t *testing.T) {
	r := newTestRegistry()
	got := ids(r.Enabled(nil, time.Now(), time.Hour))
	if len(got) != 1 || got[0] != "actions" {
		t.Fatalf("nil license should enable only CE plugins, got %v", got)
	}
}

func TestEnabledIncludesEEWithActiveLicense(t *testing.T) {
	now := time.Now()
	r := newTestRegistry()
	got := ids(r.Enabled(activeBusinessLicense(t, now), now, time.Hour))
	// Sorted by ID: access, actions, audit.
	want := []string{"access", "actions", "audit"}
	if len(got) != len(want) {
		t.Fatalf("active license should enable all plugins, got %v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("expected sorted %v, got %v", want, got)
		}
	}
}

func TestRegisterIsIdempotentLastWins(t *testing.T) {
	r := NewRegistry()
	r.Register(stubPlugin{id: "audit", edition: license.Community})
	r.Register(stubPlugin{id: "audit", edition: license.Business})
	// Now EE: should be hidden without a license.
	if got := ids(r.Enabled(nil, time.Now(), 0)); len(got) != 0 {
		t.Fatalf("re-registered EE plugin should be gated, got %v", got)
	}
}
