package registry

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"testing"
	"time"

	"github.com/catenahq/catena-ce/license"
	"github.com/catenahq/catena-ce/plugin"
)

// stubPlugin structurally satisfies plugin.Plugin (the registry stores
// plugin.Plugin; any type with these methods registers fine).
type stubPlugin struct {
	id      string
	edition license.Edition
}

func (s stubPlugin) ID() string                             { return s.id }
func (s stubPlugin) Title() string                          { return s.id }
func (s stubPlugin) Edition() license.Edition               { return s.edition }
func (s stubPlugin) Render(context.Context) (string, error) { return s.id, nil }
func (s stubPlugin) Actions() []plugin.ActionSpec           { return nil }

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

func ids(ps []plugin.Plugin) []string {
	out := make([]string, len(ps))
	for i, p := range ps {
		out[i] = p.ID()
	}
	return out
}

func newTestRegistry() *Registry {
	r := New()
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
	want := []string{"access", "actions", "audit"} // sorted by ID
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
	r := New()
	r.Register(stubPlugin{id: "audit", edition: license.Community})
	r.Register(stubPlugin{id: "audit", edition: license.Business})
	if got := ids(r.Enabled(nil, time.Now(), 0)); len(got) != 0 {
		t.Fatalf("re-registered EE plugin should be gated, got %v", got)
	}
}
