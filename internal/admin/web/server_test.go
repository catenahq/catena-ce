package web

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newTestHandler(t *testing.T) http.Handler {
	t.Helper()
	h, err := New(Config{
		Version: "test",
		Globals: map[string]any{"clients_tab_enabled": false},
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return h
}

func TestHealth(t *testing.T) {
	h := newTestHandler(t)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest("GET", "/health", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rr.Code)
	}
	if !strings.Contains(rr.Body.String(), `"version":"test"`) {
		t.Errorf("health body = %q, want version test", rr.Body.String())
	}
}

func TestRootRedirectsToApps(t *testing.T) {
	h := newTestHandler(t)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest("GET", "/", nil))
	if rr.Code != http.StatusSeeOther {
		t.Fatalf("status = %d, want 303", rr.Code)
	}
	if loc := rr.Header().Get("Location"); loc != "/apps" {
		t.Errorf("Location = %q, want /apps", loc)
	}
}

func TestAppsRendersLocalized(t *testing.T) {
	h := newTestHandler(t)
	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/apps", nil)
	req.AddCookie(&http.Cookie{Name: "catena_lang", Value: "fr"})
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rr.Code)
	}
	body := rr.Body.String()
	if !strings.Contains(body, `lang="fr"`) {
		t.Errorf("expected lang=fr in html, got %q", firstLine(body))
	}
	if !strings.Contains(body, "<title>") {
		t.Error("expected a rendered base layout with <title>")
	}
}

func TestSecurityHeaders(t *testing.T) {
	h := newTestHandler(t)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest("GET", "/apps", nil))
	if csp := rr.Header().Get("Content-Security-Policy"); !strings.Contains(csp, "default-src 'self'") {
		t.Errorf("CSP = %q, want default-src 'self'", csp)
	}
	if rr.Header().Get("X-Frame-Options") != "DENY" {
		t.Error("expected X-Frame-Options DENY")
	}
	if rr.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Error("expected X-Content-Type-Options nosniff")
	}
}

func TestNavGatedByAdmin(t *testing.T) {
	h := newTestHandler(t)

	// Non-admin: only the Apps tab.
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest("GET", "/apps", nil))
	if strings.Contains(rr.Body.String(), `href="/system"`) {
		t.Error("non-admin must not see the System tab")
	}

	// Admin: the admin tabs render.
	rr = httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/apps", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "admin")
	h.ServeHTTP(rr, req)
	if !strings.Contains(rr.Body.String(), `href="/system"`) {
		t.Error("admin must see the System tab")
	}
}

func TestSetLocaleCookieAndRedirect(t *testing.T) {
	h := newTestHandler(t)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest("GET", "/_/lang/fr?back=/apps", nil))
	if rr.Code != http.StatusSeeOther {
		t.Fatalf("status = %d, want 303", rr.Code)
	}
	if loc := rr.Header().Get("Location"); loc != "/apps" {
		t.Errorf("Location = %q, want /apps", loc)
	}
	if !strings.Contains(rr.Header().Get("Set-Cookie"), "catena_lang=fr") {
		t.Errorf("expected catena_lang=fr cookie, got %q", rr.Header().Get("Set-Cookie"))
	}
}

func TestSetLocaleRejectsUnsupported(t *testing.T) {
	h := newTestHandler(t)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest("GET", "/_/lang/de", nil))
	if !strings.Contains(rr.Header().Get("Set-Cookie"), "catena_lang=en") {
		t.Errorf("unsupported locale should fall back to en, got %q", rr.Header().Get("Set-Cookie"))
	}
}

func TestSetThemeCycle(t *testing.T) {
	h := newTestHandler(t)
	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/_/theme/cycle?back=/apps", nil)
	req.AddCookie(&http.Cookie{Name: "catena_theme", Value: "light"})
	h.ServeHTTP(rr, req)
	if !strings.Contains(rr.Header().Get("Set-Cookie"), "catena_theme=dark") {
		t.Errorf("cycle from light should set dark, got %q", rr.Header().Get("Set-Cookie"))
	}
}

func TestSafeBack(t *testing.T) {
	cases := map[string]string{
		"/apps":            "/apps",
		"//evil.com":       "/",
		"https://evil.com": "/",
		"":                 "/",
		"relative":         "/",
	}
	for in, want := range cases {
		if got := safeBack(in); got != want {
			t.Errorf("safeBack(%q) = %q, want %q", in, got, want)
		}
	}
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}
