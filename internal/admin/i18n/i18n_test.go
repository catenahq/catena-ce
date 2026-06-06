package i18n

import (
	"net/http"
	"os"
	"path/filepath"
	"testing"
)

func writeFixtures(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	en := "tabs:\n  apps: Apps\n  system: System\ngreeting: Hello {name}\nonly_en: present\n"
	fr := "tabs:\n  apps: Applications\n  system: Système\ngreeting: Bonjour {name}\n"
	if err := os.WriteFile(filepath.Join(dir, "en.yml"), []byte(en), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "fr.yml"), []byte(fr), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestGetFlattenedKeys(t *testing.T) {
	tr, err := Load(writeFixtures(t))
	if err != nil {
		t.Fatal(err)
	}
	if got := tr.Get("tabs.apps", "fr", nil); got != "Applications" {
		t.Errorf("fr tabs.apps = %q, want Applications", got)
	}
	if got := tr.Get("tabs.system", "en", nil); got != "System" {
		t.Errorf("en tabs.system = %q, want System", got)
	}
}

func TestGetFallsBackToEn(t *testing.T) {
	tr, err := Load(writeFixtures(t))
	if err != nil {
		t.Fatal(err)
	}
	// only_en exists in en, not fr -> fall back to the en value.
	if got := tr.Get("only_en", "fr", nil); got != "present" {
		t.Errorf("fr only_en = %q, want en fallback 'present'", got)
	}
}

func TestGetMissingReturnsKey(t *testing.T) {
	tr, err := Load(writeFixtures(t))
	if err != nil {
		t.Fatal(err)
	}
	if got := tr.Get("nope.missing", "en", nil); got != "nope.missing" {
		t.Errorf("missing key = %q, want the key itself", got)
	}
}

func TestGetInterpolates(t *testing.T) {
	tr, err := Load(writeFixtures(t))
	if err != nil {
		t.Fatal(err)
	}
	if got := tr.Get("greeting", "fr", map[string]string{"name": "Marc"}); got != "Bonjour Marc" {
		t.Errorf("interpolated = %q, want 'Bonjour Marc'", got)
	}
}

func TestMissingFromTarget(t *testing.T) {
	tr, err := Load(writeFixtures(t))
	if err != nil {
		t.Fatal(err)
	}
	missing := tr.MissingFromTarget("fr")
	if len(missing) != 1 || missing[0] != "only_en" {
		t.Errorf("MissingFromTarget(fr) = %v, want [only_en]", missing)
	}
}

func TestLoadMissingFileIsEmptyNotError(t *testing.T) {
	dir := t.TempDir() // no yml files at all
	tr, err := Load(dir)
	if err != nil {
		t.Fatalf("Load on empty dir should not error: %v", err)
	}
	if got := tr.Get("anything", "en", nil); got != "anything" {
		t.Errorf("empty store should return the key, got %q", got)
	}
}

func TestResolveLocale(t *testing.T) {
	t.Setenv(DefaultLocaleEnv, "")
	cases := []struct {
		name   string
		build  func() *http.Request
		want   string
	}{
		{"query wins", func() *http.Request {
			r, _ := http.NewRequest("GET", "/?lang=fr", nil)
			r.AddCookie(&http.Cookie{Name: CookieName, Value: "en"})
			return r
		}, "fr"},
		{"cookie", func() *http.Request {
			r, _ := http.NewRequest("GET", "/", nil)
			r.AddCookie(&http.Cookie{Name: CookieName, Value: "fr"})
			return r
		}, "fr"},
		{"accept-language family", func() *http.Request {
			r, _ := http.NewRequest("GET", "/", nil)
			r.Header.Set("Accept-Language", "fr-CA,fr;q=0.9,en;q=0.8")
			return r
		}, "fr"},
		{"fallback en", func() *http.Request {
			r, _ := http.NewRequest("GET", "/", nil)
			return r
		}, "en"},
		{"unsupported query ignored", func() *http.Request {
			r, _ := http.NewRequest("GET", "/?lang=de", nil)
			return r
		}, "en"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := ResolveLocale(tc.build()); got != tc.want {
				t.Errorf("ResolveLocale = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestResolveLocaleEnvDefault(t *testing.T) {
	t.Setenv(DefaultLocaleEnv, "fr")
	r, _ := http.NewRequest("GET", "/", nil)
	if got := ResolveLocale(r); got != "fr" {
		t.Errorf("env default = %q, want fr", got)
	}
}
