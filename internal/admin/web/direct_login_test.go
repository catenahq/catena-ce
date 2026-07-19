package web

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/catenahq/catena-ce/internal/admin/auth"
)

func directHandler(t *testing.T, storeJSON string) http.Handler {
	t.Helper()
	_, direct, err := NewWithDirect(
		Config{Version: "t", Runner: &fakeRunner{stdout: []string{storeJSON}}},
		DirectConfig{SessionKey: "test-session-key", LocalUser: "op@example.com"},
	)
	if err != nil {
		t.Fatalf("NewWithDirect: %v", err)
	}
	if direct == nil {
		t.Fatal("direct handler nil despite a session key")
	}
	return direct
}

func TestDirectHandlerNilWithoutSessionKey(t *testing.T) {
	proxy, direct, err := NewWithDirect(Config{Version: "t"}, DirectConfig{})
	if err != nil {
		t.Fatal(err)
	}
	if proxy == nil {
		t.Error("proxy handler must always be built")
	}
	if direct != nil {
		t.Error("direct handler must be nil when no session key is configured")
	}
}

func TestDirectLoginPageServed(t *testing.T) {
	h := directHandler(t, "{}")
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest("GET", "/login", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("GET /login = %d, want 200", rr.Code)
	}
	if !strings.Contains(rr.Body.String(), `action="/login"`) {
		t.Error("login page missing the login form")
	}
}

func TestDirectUnauthenticatedRedirectsToLogin(t *testing.T) {
	h := directHandler(t, "{}")
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest("GET", "/settings", nil))
	if rr.Code != http.StatusSeeOther {
		t.Fatalf("unauth GET /settings = %d, want 303", rr.Code)
	}
	if loc := rr.Header().Get("Location"); loc != "/login" {
		t.Errorf("Location = %q, want /login", loc)
	}
}

func TestDirectForgedHeaderDoesNotAuthenticate(t *testing.T) {
	h := directHandler(t, "{}")
	rr := httptest.NewRecorder()
	// A tailnet peer forging the proxy identity headers must NOT be trusted on
	// the direct listener -- trust is by socket, not header.
	req := httptest.NewRequest("GET", "/settings", nil)
	req.Header.Set("X-Forwarded-Email", "attacker@example.com")
	req.Header.Set("X-Forwarded-Groups", "admin")
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusSeeOther {
		t.Fatalf("forged-header GET /settings = %d, want 303 redirect to login", rr.Code)
	}
}

func TestDirectLoginFlow(t *testing.T) {
	store := `{"secrets":{"vault_admin_password":"s3cret-pw"},"config":{}}`
	h := directHandler(t, store)

	// Wrong password is rejected.
	bad := httptest.NewRecorder()
	h.ServeHTTP(bad, formReq("op@example.com", "wrong"))
	if bad.Code != http.StatusUnauthorized {
		t.Fatalf("bad login = %d, want 401", bad.Code)
	}

	// Correct credentials set a session cookie and redirect to /apps.
	ok := httptest.NewRecorder()
	h.ServeHTTP(ok, formReq("op@example.com", "s3cret-pw"))
	if ok.Code != http.StatusSeeOther {
		t.Fatalf("good login = %d, want 303", ok.Code)
	}
	if loc := ok.Header().Get("Location"); loc != "/apps" {
		t.Errorf("Location = %q, want /apps", loc)
	}
	cookie := sessionCookie(t, ok.Result().Cookies())

	// The session cookie grants admin access to /settings.
	authed := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/settings", nil)
	req.AddCookie(cookie)
	h.ServeHTTP(authed, req)
	if authed.Code != http.StatusOK {
		t.Fatalf("authed GET /settings = %d, want 200", authed.Code)
	}
}

func formReq(user, pass string) *http.Request {
	body := "username=" + user + "&password=" + pass
	r := httptest.NewRequest("POST", "/login", strings.NewReader(body))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	return r
}

func sessionCookie(t *testing.T, cookies []*http.Cookie) *http.Cookie {
	t.Helper()
	for _, c := range cookies {
		if c.Name == auth.SessionCookieName {
			return c
		}
	}
	t.Fatal("no session cookie set on successful login")
	return nil
}
