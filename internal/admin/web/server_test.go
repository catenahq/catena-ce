package web

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/catenahq/catena-ce/internal/admin/actions"
	"github.com/catenahq/catena-ce/internal/admin/integrations"
)

type fakeDok struct{ items []integrations.DokployItem }

func (f fakeDok) ListItems(bool) []integrations.DokployItem { return f.items }

type fakeGatusReader struct{}

func (fakeGatusReader) ListStatuses() []integrations.EndpointStatus { return nil }
func (fakeGatusReader) GetStatusByHost(string) (integrations.EndpointStatus, bool) {
	return integrations.EndpointStatus{}, false
}

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

	// Admin: the CE admin tabs render, but the EE tabs stay hidden in
	// Community (no per-tab globals set).
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", "/apps", ""))
	body := rr.Body.String()
	if !strings.Contains(body, `href="/system"`) {
		t.Error("admin must see the System tab")
	}
	for _, ee := range []string{`href="/access"`, `href="/daily"`, `href="/audit"`} {
		if strings.Contains(body, ee) {
			t.Errorf("CE-only nav must not show the EE link %s", ee)
		}
	}
}

func TestEETabsShownWhenGlobalsEnabled(t *testing.T) {
	h, err := New(Config{
		Version: "t",
		Globals: map[string]any{
			"access_tab_enabled": true,
			"daily_tab_enabled":  true,
			"audit_tab_enabled":  true,
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", "/apps", ""))
	body := rr.Body.String()
	for _, ee := range []string{`href="/access"`, `href="/daily"`, `href="/audit"`} {
		if !strings.Contains(body, ee) {
			t.Errorf("enabled EE tab link %s should render", ee)
		}
	}
}

func TestActionsRequiresAdminAndRenders(t *testing.T) {
	h := newTestHandler(t)

	// Non-admin -> 403.
	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/actions", nil)
	req.Header.Set("X-Forwarded-Email", "staff@example.com")
	req.Header.Set("X-Forwarded-Groups", "staff")
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusForbidden {
		t.Fatalf("non-admin /actions = %d, want 403", rr.Code)
	}

	// Admin -> 200, empty-state (no catalog file configured in the test).
	rr = httptest.NewRecorder()
	req = httptest.NewRequest("GET", "/actions", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "admin")
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("admin /actions = %d, want 200", rr.Code)
	}
	if !strings.Contains(rr.Body.String(), "actions-page") {
		t.Error("expected the Actions page to render")
	}
}

func TestActionsIncludesPluginActions(t *testing.T) {
	// CE catalog has backup-now (Backups); an EE plugin contributes audit-export (Ops).
	h, err := New(Config{
		Version:     "t",
		ActionsFile: writeActionsFile(t),
		PluginActions: func() []actions.Action {
			return []actions.Action{{
				Name: "audit-export", Title: "Export audit log",
				Category: "Ops", Source: "audit",
			}}
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", "/actions", ""))
	if rr.Code != http.StatusOK {
		t.Fatalf("admin /actions = %d, want 200", rr.Code)
	}
	body := rr.Body.String()
	if !strings.Contains(body, "Export audit log") {
		t.Error("expected the EE plugin action to render on the Actions tab")
	}
	if !strings.Contains(body, "Backup now") {
		t.Error("expected the CE catalog action to still render alongside it")
	}
}

func TestPluginActionDispatchesThroughSameRunner(t *testing.T) {
	fr := &fakeRunner{stdout: []string{"exported"}, rc: 0}
	h, err := New(Config{
		Version:     "t",
		ActionsFile: writeActionsFile(t),
		Runner:      fr,
		PluginActions: func() []actions.Action {
			return []actions.Action{{
				Name: "audit-export", Title: "Export audit log",
				Category: "Ops", Source: "audit", Shell: "catena-audit export",
			}}
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	// A plugin-contributed action starts + streams through the shell's own
	// runner -- the plugin never holds the key.
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("POST", "/actions/start/audit-export", ""))
	if rr.Code != http.StatusOK {
		t.Fatalf("plugin action start = %d, want 200", rr.Code)
	}
	streamURL := extractStreamURL(t, rr.Body.String())
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", streamURL, ""))
	if !strings.Contains(rr.Body.String(), "data: exported\n\n") {
		t.Errorf("expected the dispatched plugin-action output, got %q", rr.Body.String())
	}
}

func auditPanelConfig() Config {
	return Config{
		Version: "t",
		Panels: func() []PanelInfo {
			return []PanelInfo{{
				ID: "audit", Title: "Audit log",
				Render: func(context.Context) (string, error) {
					return "<p>recent administrative actions</p>", nil
				},
			}}
		},
	}
}

func TestPluginNavLinkRendersForEnabledPanel(t *testing.T) {
	h, err := New(auditPanelConfig())
	if err != nil {
		t.Fatal(err)
	}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", "/apps", ""))
	body := rr.Body.String()
	if !strings.Contains(body, `href="/plugin/audit"`) || !strings.Contains(body, "Audit log") {
		t.Errorf("expected the enabled plugin nav link, got %q", firstLine(body))
	}
}

func TestPluginNavLinkHiddenWhenNoPanels(t *testing.T) {
	h := newTestHandler(t) // no Panels provider -> Community
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", "/apps", ""))
	if strings.Contains(rr.Body.String(), `/plugin/`) {
		t.Error("CE-only host must show no plugin nav links")
	}
}

func TestPluginPanelRenders(t *testing.T) {
	h, err := New(auditPanelConfig())
	if err != nil {
		t.Fatal(err)
	}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", "/plugin/audit", ""))
	if rr.Code != http.StatusOK {
		t.Fatalf("admin /plugin/audit = %d, want 200", rr.Code)
	}
	if !strings.Contains(rr.Body.String(), "recent administrative actions") {
		t.Error("expected the plugin panel body to render")
	}
}

func TestPluginPanelUnknownIs404(t *testing.T) {
	h, err := New(auditPanelConfig())
	if err != nil {
		t.Fatal(err)
	}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", "/plugin/ghost", ""))
	if rr.Code != http.StatusNotFound {
		t.Fatalf("unknown plugin panel = %d, want 404", rr.Code)
	}
}

func TestPluginPanelRequiresAdmin(t *testing.T) {
	h, err := New(auditPanelConfig())
	if err != nil {
		t.Fatal(err)
	}
	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/plugin/audit", nil)
	req.Header.Set("X-Forwarded-Email", "staff@example.com")
	req.Header.Set("X-Forwarded-Groups", "staff")
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusForbidden {
		t.Fatalf("non-admin /plugin/audit = %d, want 403", rr.Code)
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

func TestAppsGridRendersTiles(t *testing.T) {
	dok := fakeDok{items: []integrations.DokployItem{{
		Kind: "compose", ItemID: "c1", AppName: "Nextcloud",
		Domains: []integrations.Domain{{Host: "cloud.example.com"}},
		ComposeBody: `    labels:
      - "vps.auth.mode=public"
      - "vps.homepage.name=Cloud"
`,
	}}}
	h, err := New(Config{
		Version:        "t",
		Globals:        map[string]any{"self_app_id": ""},
		Dokploy:        dok,
		Gatus:          fakeGatusReader{},
		ExtraTilesPath: "/nonexistent",
	})
	if err != nil {
		t.Fatal(err)
	}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest("GET", "/apps", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rr.Code)
	}
	body := rr.Body.String()
	if !strings.Contains(body, "tile-grid") || !strings.Contains(body, "Cloud") {
		t.Errorf("expected a tile grid with the public tile, got %q", firstLine(body))
	}
}

func TestDashboardRequiresAuthAndRenders(t *testing.T) {
	h := newTestHandler(t)

	// Anonymous visitor (no email header) -> 403.
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest("GET", "/dashboard", nil))
	if rr.Code != http.StatusForbidden {
		t.Fatalf("anonymous /dashboard = %d, want 403", rr.Code)
	}

	// A signed-in client (not admin) -> 200: the dashboard is client-facing.
	rr = httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/dashboard", nil)
	req.Header.Set("X-Forwarded-Email", "user@example.com")
	req.Header.Set("X-Forwarded-Groups", "client")
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("client /dashboard = %d, want 200", rr.Code)
	}
	if !strings.Contains(rr.Body.String(), "system-page") {
		t.Error("expected the dashboard card grid to render")
	}
}

func TestDashboardNavVisibleToAuthenticated(t *testing.T) {
	h := newTestHandler(t)

	// Anonymous: no Dashboard tab.
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, httptest.NewRequest("GET", "/apps", nil))
	if strings.Contains(rr.Body.String(), `href="/dashboard"`) {
		t.Error("anonymous visitor must not see the Dashboard tab")
	}

	// Signed-in client: the Dashboard tab renders, the admin tabs do not.
	rr = httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/apps", nil)
	req.Header.Set("X-Forwarded-Email", "user@example.com")
	req.Header.Set("X-Forwarded-Groups", "client")
	h.ServeHTTP(rr, req)
	body := rr.Body.String()
	if !strings.Contains(body, `href="/dashboard"`) {
		t.Error("a signed-in client must see the Dashboard tab")
	}
	if strings.Contains(body, `href="/system"`) {
		t.Error("a non-admin must not see the System tab")
	}
}

func TestSystemRequiresAdmin(t *testing.T) {
	h := newTestHandler(t)

	// Non-admin -> 403.
	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/system", nil)
	req.Header.Set("X-Forwarded-Email", "staff@example.com")
	req.Header.Set("X-Forwarded-Groups", "staff")
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusForbidden {
		t.Fatalf("non-admin /system = %d, want 403", rr.Code)
	}

	// Admin -> 200, renders the snapshot (nil clients degrade gracefully).
	rr = httptest.NewRecorder()
	req = httptest.NewRequest("GET", "/system", nil)
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "admin")
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("admin /system = %d, want 200", rr.Code)
	}
	if !strings.Contains(rr.Body.String(), "infra-list") {
		t.Error("expected the System tab infra rollup to render")
	}
}

type fakeRunner struct {
	stdout []string
	stderr []string
	rc     int
	gotCmd string
	gotEnv map[string]string
}

func (f *fakeRunner) Run(_ context.Context, command string, env map[string]string, onStdout, onStderr func(string)) (int, error) {
	f.gotCmd = command
	f.gotEnv = env
	for _, l := range f.stdout {
		onStdout(l)
	}
	for _, l := range f.stderr {
		onStderr(l)
	}
	return f.rc, nil
}

func writeActionsFile(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "admin-actions.yml")
	body := "actions:\n" +
		"  - name: backup-now\n" +
		"    title: Backup now\n" +
		"    category: Backups\n"
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func adminReq(method, target string, body string) *http.Request {
	var r *http.Request
	if body != "" {
		r = httptest.NewRequest(method, target, strings.NewReader(body))
		r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	} else {
		r = httptest.NewRequest(method, target, nil)
	}
	r.Header.Set("X-Forwarded-Email", "op@example.com")
	r.Header.Set("X-Forwarded-Groups", "admin")
	return r
}

func TestActionsStartRequiresAdmin(t *testing.T) {
	fr := &fakeRunner{}
	h, err := New(Config{Version: "t", ActionsFile: writeActionsFile(t), Runner: fr})
	if err != nil {
		t.Fatal(err)
	}
	rr := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/actions/start/backup-now", nil)
	req.Header.Set("X-Forwarded-Email", "staff@example.com")
	req.Header.Set("X-Forwarded-Groups", "staff")
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusForbidden {
		t.Fatalf("non-admin start = %d, want 403", rr.Code)
	}
}

func TestActionsStartUnknownAction(t *testing.T) {
	h, err := New(Config{Version: "t", ActionsFile: writeActionsFile(t), Runner: &fakeRunner{}})
	if err != nil {
		t.Fatal(err)
	}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("POST", "/actions/start/does-not-exist", ""))
	if rr.Code != http.StatusNotFound {
		t.Fatalf("unknown action start = %d, want 404", rr.Code)
	}
}

func TestActionsStartThenStream(t *testing.T) {
	fr := &fakeRunner{stdout: []string{"line1", "line2"}, stderr: []string{"warn"}, rc: 0}
	h, err := New(Config{Version: "t", ActionsFile: writeActionsFile(t), Runner: fr})
	if err != nil {
		t.Fatal(err)
	}

	// Start: returns the run-panel fragment with a stream URL.
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("POST", "/actions/start/backup-now", "payload=arg1"))
	if rr.Code != http.StatusOK {
		t.Fatalf("start = %d, want 200", rr.Code)
	}
	body := rr.Body.String()
	if !strings.Contains(body, `class="run-panel"`) {
		t.Fatalf("expected the run-panel fragment, got %q", body)
	}
	streamURL := extractStreamURL(t, body)

	// Stream: SSE frames, command + forwarded email reach the runner.
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", streamURL, ""))
	if rr.Code != http.StatusOK {
		t.Fatalf("stream = %d, want 200", rr.Code)
	}
	if ct := rr.Header().Get("Content-Type"); ct != "text/event-stream" {
		t.Errorf("Content-Type = %q, want text/event-stream", ct)
	}
	out := rr.Body.String()
	for _, want := range []string{"data: line1\n\n", "data: line2\n\n", "event: stderr\ndata: warn\n\n", "event: end\ndata: 0\n\n"} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in stream:\n%s", want, out)
		}
	}
	if fr.gotCmd != "backup-now arg1" {
		t.Errorf("command = %q, want 'backup-now arg1'", fr.gotCmd)
	}
	if fr.gotEnv["X_FORWARDED_EMAIL"] != "op@example.com" {
		t.Errorf("X_FORWARDED_EMAIL = %q, want op@example.com", fr.gotEnv["X_FORWARDED_EMAIL"])
	}

	// One-shot: a second stream of the same job is 404.
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", streamURL, ""))
	if rr.Code != http.StatusNotFound {
		t.Fatalf("re-stream = %d, want 404 (one-shot)", rr.Code)
	}
}

func TestDispatchEmitsAuditRow(t *testing.T) {
	fr := &fakeRunner{stdout: []string{"done"}, rc: 0}
	var got struct {
		action, email, ip string
		rc                int
		calls             int
	}
	h, err := New(Config{
		Version:     "t",
		ActionsFile: writeActionsFile(t),
		Runner:      fr,
		AuditEmit: func(action, email, ip string, rc int) {
			got.action, got.email, got.ip, got.rc = action, email, ip, rc
			got.calls++
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("POST", "/actions/start/backup-now", ""))
	streamURL := extractStreamURL(t, rr.Body.String())
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", streamURL, ""))

	if got.calls != 1 {
		t.Fatalf("expected exactly one audit emit, got %d", got.calls)
	}
	if got.action != "backup-now" || got.email != "op@example.com" || got.rc != 0 {
		t.Fatalf("unexpected audit row: %+v", got)
	}
}

func TestActionsStreamUnknownJob(t *testing.T) {
	h, err := New(Config{Version: "t", ActionsFile: writeActionsFile(t), Runner: &fakeRunner{}})
	if err != nil {
		t.Fatal(err)
	}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", "/actions/stream/nope", ""))
	if rr.Code != http.StatusNotFound {
		t.Fatalf("unknown job stream = %d, want 404", rr.Code)
	}
}

func TestActionsStreamNilRunner(t *testing.T) {
	// No runner configured: start still works, the stream emits an error frame.
	h, err := New(Config{Version: "t", ActionsFile: writeActionsFile(t)})
	if err != nil {
		t.Fatal(err)
	}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("POST", "/actions/start/backup-now", ""))
	streamURL := extractStreamURL(t, rr.Body.String())

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", streamURL, ""))
	if !strings.Contains(rr.Body.String(), "no dispatcher configured") {
		t.Errorf("expected a no-dispatcher frame, got %q", rr.Body.String())
	}
}

func writeMixedActionsFile(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "admin-actions.yml")
	body := "actions:\n" +
		"  - name: backup-now\n" +
		"    title: Backup now\n" +
		"    category: Backups\n" +
		"  - name: generate-recovery-archive\n" +
		"    title: Generate recovery archive\n" +
		"    category: Recovery\n"
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestRecoveryRequiresAdminAndRenders(t *testing.T) {
	h, err := New(Config{Version: "t", ActionsFile: writeMixedActionsFile(t), ExportsDir: "/nonexistent"})
	if err != nil {
		t.Fatal(err)
	}
	// Non-admin -> 403.
	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/recovery", nil)
	req.Header.Set("X-Forwarded-Email", "staff@example.com")
	req.Header.Set("X-Forwarded-Groups", "staff")
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusForbidden {
		t.Fatalf("non-admin /recovery = %d, want 403", rr.Code)
	}
	// Admin -> 200, empty downloads + the Recovery generate button.
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", "/recovery", ""))
	if rr.Code != http.StatusOK {
		t.Fatalf("admin /recovery = %d, want 200", rr.Code)
	}
	body := rr.Body.String()
	if !strings.Contains(body, "recovery-page") {
		t.Error("expected the Recovery page to render")
	}
	if !strings.Contains(body, "/recovery/start/generate-recovery-archive") {
		t.Error("expected the Recovery-category generate button")
	}
	// The Backups action must NOT appear on the Recovery tab.
	if strings.Contains(body, "/recovery/start/backup-now") {
		t.Error("a Backups action leaked onto the Recovery tab")
	}
}

func TestRecoveryStartScopedToRecovery(t *testing.T) {
	fr := &fakeRunner{stdout: []string{"archiving"}, rc: 0}
	h, err := New(Config{Version: "t", ActionsFile: writeMixedActionsFile(t), Runner: fr})
	if err != nil {
		t.Fatal(err)
	}
	// A Backups (Actions-tab) action cannot be fired from /recovery/start.
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("POST", "/recovery/start/backup-now", ""))
	if rr.Code != http.StatusNotFound {
		t.Fatalf("cross-tab /recovery/start/backup-now = %d, want 404", rr.Code)
	}
	// The Recovery action starts and streams via the /recovery/stream prefix.
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("POST", "/recovery/start/generate-recovery-archive", ""))
	if rr.Code != http.StatusOK {
		t.Fatalf("recovery start = %d, want 200", rr.Code)
	}
	streamURL := extractStreamURL(t, rr.Body.String())
	if !strings.HasPrefix(streamURL, "/recovery/stream/") {
		t.Fatalf("stream URL = %q, want /recovery/stream/ prefix", streamURL)
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", streamURL, ""))
	if !strings.Contains(rr.Body.String(), "data: archiving\n\n") {
		t.Errorf("expected the dispatched stdout frame, got %q", rr.Body.String())
	}
}

func TestMaintenanceRequiresAdminAndRenders(t *testing.T) {
	h := newTestHandler(t)
	// Non-admin -> 403.
	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/maintenance", nil)
	req.Header.Set("X-Forwarded-Email", "staff@example.com")
	req.Header.Set("X-Forwarded-Groups", "staff")
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusForbidden {
		t.Fatalf("non-admin /maintenance = %d, want 403", rr.Code)
	}
	// Admin -> 200, empty state (no log file on the test host).
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", "/maintenance", ""))
	if rr.Code != http.StatusOK {
		t.Fatalf("admin /maintenance = %d, want 200", rr.Code)
	}
	if !strings.Contains(rr.Body.String(), "maintenance-page") {
		t.Error("expected the Maintenance page to render")
	}
}

func TestResourcesDisabledAndEnabled(t *testing.T) {
	// No beszel_url -> the disabled state.
	h := newTestHandler(t)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, adminReq("GET", "/resources", ""))
	if rr.Code != http.StatusOK {
		t.Fatalf("admin /resources = %d, want 200", rr.Code)
	}
	if strings.Contains(rr.Body.String(), "<iframe") {
		t.Error("no beszel_url should render the disabled state, not an iframe")
	}

	// beszel_url set -> the embed.
	h2, err := New(Config{Version: "t", Globals: map[string]any{"beszel_url": "https://hub.example.com"}})
	if err != nil {
		t.Fatal(err)
	}
	rr = httptest.NewRecorder()
	h2.ServeHTTP(rr, adminReq("GET", "/resources", ""))
	body := rr.Body.String()
	if !strings.Contains(body, "<iframe") || !strings.Contains(body, "https://hub.example.com") {
		t.Errorf("expected the Beszel embed, got %q", firstLine(body))
	}
}

// extractStreamURL pulls the sse-connect URL out of the run-panel fragment.
func extractStreamURL(t *testing.T, body string) string {
	t.Helper()
	const marker = `sse-connect="`
	i := strings.Index(body, marker)
	if i < 0 {
		t.Fatalf("no sse-connect in fragment: %q", body)
	}
	rest := body[i+len(marker):]
	j := strings.IndexByte(rest, '"')
	if j < 0 {
		t.Fatalf("unterminated sse-connect: %q", body)
	}
	url := rest[:j]
	if !strings.Contains(url, "/stream/") {
		t.Fatalf("unexpected stream URL %q", url)
	}
	return url
}

// ensure the actions.Runner interface is what the fake satisfies.
var _ actions.Runner = (*fakeRunner)(nil)

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}
