package web

import (
	"encoding/json"
	"fmt"
	"io/fs"
	"net/http"
	"net/url"
	"os"
	"strings"

	"github.com/catenahq/catena-ce/internal/admin/actions"
	"github.com/catenahq/catena-ce/internal/admin/apps"
	"github.com/catenahq/catena-ce/internal/admin/i18n"
	"github.com/catenahq/catena-ce/internal/admin/integrations"
	"github.com/catenahq/catena-ce/internal/admin/system"
	"github.com/catenahq/catena-ce/internal/admin/theme"
)

// GatusReader is the Gatus surface the shell needs: the System tab lists all
// statuses, the Apps tab looks one up by host. The concrete GatusClient
// satisfies both.
type GatusReader interface {
	ListStatuses() []integrations.EndpointStatus
	GetStatusByHost(host string) (integrations.EndpointStatus, bool)
}

// Config wires the shell server. Globals are exposed to every template
// (feature gates + deep-link URLs); an empty TranslationsDir uses the
// embedded EN/FR copy (the bind-mount override path the Python app had).
type Config struct {
	Version         string
	Globals         map[string]any
	TranslationsDir string
	// Gatus + Healthchecks feed the System tab (and Apps tile dots). Either
	// may be nil on an unconfigured host -- the panels degrade gracefully.
	Gatus        GatusReader
	Healthchecks system.HCLister
	// Dokploy feeds the Apps tile grid; nil renders an empty grid.
	Dokploy apps.DokployLister
	// StatsDir overrides the host stats dir (default /var/lib/catena).
	StatsDir string
	// ExtraTilesPath overrides the operator extra-tiles.yml location.
	ExtraTilesPath string
	// ActionsFile overrides the host action catalog (admin-actions.yml).
	ActionsFile string
	// Runner dispatches Actions/Recovery commands to the host. Nil disables
	// dispatch -- the stream emits a "no dispatcher configured" frame rather
	// than panicking, so the tab still renders on a host without SSH wired.
	Runner actions.Runner
}

// New builds the shell HTTP handler: static mount, public routes, and the
// CE tab routes (added as they are ported), wrapped in the security-headers
// and request-state middleware.
func New(cfg Config) (http.Handler, error) {
	tr, err := loadTranslations(cfg.TranslationsDir)
	if err != nil {
		return nil, err
	}
	if cfg.Globals == nil {
		cfg.Globals = map[string]any{}
	}
	tmpl, err := NewTemplates(tr, cfg.Globals)
	if err != nil {
		return nil, err
	}
	s := &server{
		tmpl:           tmpl,
		version:        cfg.Version,
		gatus:          cfg.Gatus,
		hc:             cfg.Healthchecks,
		dokploy:        cfg.Dokploy,
		statsDir:       cfg.StatsDir,
		extraTilesPath: cfg.ExtraTilesPath,
		actionsFile:    cfg.ActionsFile,
		runner:         cfg.Runner,
		jobs:           actions.NewJobRegistry(),
	}

	mux := http.NewServeMux()
	staticSub, err := fs.Sub(staticFS, "static")
	if err != nil {
		return nil, err
	}
	mux.Handle("GET /_/static/", http.StripPrefix("/_/static/", http.FileServer(http.FS(staticSub))))
	mux.HandleFunc("GET /health", s.health)
	mux.HandleFunc("GET /{$}", s.rootRedirect)
	mux.HandleFunc("GET /apps", s.apps)
	mux.HandleFunc("GET /system", RequireAdmin(s.systemIndex))
	mux.HandleFunc("GET /actions", RequireAdmin(s.actionsIndex))
	mux.HandleFunc("POST /actions/start/{name}", RequireAdmin(s.actionsStart))
	mux.HandleFunc("GET /actions/stream/{job_id}", RequireAdmin(s.actionsStream))
	mux.HandleFunc("GET /_/lang/{lang}", s.setLocale)
	mux.HandleFunc("GET /_/theme/{name}", s.setTheme)

	// SecurityHeaders outermost (sets headers on the way out); RequestState
	// inner (sets context on the way in). Matches the Python middleware order.
	return SecurityHeaders(RequestState(mux)), nil
}

func loadTranslations(dir string) (*i18n.Translations, error) {
	if strings.TrimSpace(dir) != "" {
		return i18n.Load(dir)
	}
	sub, err := fs.Sub(translationsFS, "translations")
	if err != nil {
		return nil, err
	}
	return i18n.LoadFS(sub)
}

type server struct {
	tmpl           *Templates
	version        string
	gatus          GatusReader
	hc             system.HCLister
	dokploy        apps.DokployLister
	statsDir       string
	extraTilesPath string
	actionsFile    string
	runner         actions.Runner
	jobs           *actions.JobRegistry
}

// actionsView is the Actions-tab render data: the catalog grouped into the
// fixed category order, plus whether any category has actions (else the
// empty-state renders).
type actionsView struct {
	Groups []actions.CategoryGroup
	HasAny bool
}

// actionsIndex renders the admin-only Actions tab: the static CE catalog
// merged with EE plugin-contributed actions (empty until the plugin SDK gains
// Actions() in M2.5), grouped by category. Recovery-category actions are
// excluded (they render on the Recovery tab).
func (s *server) actionsIndex(w http.ResponseWriter, r *http.Request) {
	catalog := actions.MergedCatalog(actions.Load(s.actionsFile), nil)
	groups := actions.GroupByCategory(actions.ForActionsTab(catalog))
	hasAny := false
	for _, g := range groups {
		if len(g.Actions) > 0 {
			hasAny = true
			break
		}
	}
	s.tmpl.Render(w, r, "actions", http.StatusOK, actionsView{Groups: groups, HasAny: hasAny})
}

// runPanelView is the run-panel fragment render data: the action being run and
// the SSE stream URL the panel connects to.
type runPanelView struct {
	ActionName string
	StreamURL  string
}

// actionsStart resolves the named action from the merged catalog, creates a
// one-shot job, and returns the run-panel fragment that connects to the SSE
// stream. An unknown action is 404 (no job created), so a stale/forged name
// cannot enqueue a dispatch.
func (s *server) actionsStart(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	catalog := actions.MergedCatalog(actions.Load(s.actionsFile), nil)
	var match *actions.Action
	for i := range catalog {
		if catalog[i].Name == name {
			match = &catalog[i]
			break
		}
	}
	if match == nil {
		http.Error(w, "unknown action", http.StatusNotFound)
		return
	}
	_ = r.ParseForm()
	id := identityFrom(r)
	job := s.jobs.Create(actions.Job{
		ActionName: match.Name,
		Payload:    r.PostForm.Get("payload"),
		Email:      id.Email,
		SourceIP:   clientIP(r),
		Category:   match.Category,
	})
	s.tmpl.RenderFragment(w, r, "run_panel", http.StatusOK, runPanelView{
		ActionName: match.Name,
		StreamURL:  "/actions/stream/" + job.ID,
	})
}

// actionsStream pops the one-shot job and streams its dispatch as SSE. A
// missing job (already streamed, expired, or forged id) is 404 -- a refresh of
// the stream URL does not re-run the action. The response is text/event-stream;
// StreamDispatch writes + flushes each frame.
func (s *server) actionsStream(w http.ResponseWriter, r *http.Request) {
	job, ok := s.jobs.Pop(r.PathValue("job_id"))
	if !ok {
		http.Error(w, "no such job", http.StatusNotFound)
		return
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	// Defeat nginx/Cloudflare response buffering so frames reach the browser
	// as they are produced, not at end-of-stream.
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	actions.StreamDispatch(w, flusher.Flush, s.runner, job.ActionName, job.Payload, job.Email)
}

// clientIP extracts the best-effort source IP for the audit row: the
// left-most X-Forwarded-For hop if present (oauth2-proxy/CF set it), else the
// transport peer.
func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		if i := strings.IndexByte(xff, ','); i >= 0 {
			return strings.TrimSpace(xff[:i])
		}
		return strings.TrimSpace(xff)
	}
	return r.RemoteAddr
}

// systemIndex renders the admin-only System tab: the backup/disk/Healthchecks
// gauges, current alerts, and the infrastructure rollup.
func (s *server) systemIndex(w http.ResponseWriter, r *http.Request) {
	snap := system.BuildSnapshot(s.gatus, s.hc, s.statsDir)
	s.tmpl.Render(w, r, "system", http.StatusOK, snap)
}

func (s *server) health(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok", "version": s.version})
}

// rootRedirect: / is the canonical landing for everyone -> /apps.
func (s *server) rootRedirect(w http.ResponseWriter, r *http.Request) {
	http.Redirect(w, r, "/apps", http.StatusSeeOther)
}

// apps is the landing tile grid: every app/compose the current identity may
// see, with live status dots. The canonical landing for everyone.
func (s *server) apps(w http.ResponseWriter, r *http.Request) {
	tiles := apps.BuildTiles(s.dokploy, s.gatus, identityFrom(r), s.extraTilesPath)
	s.tmpl.Render(w, r, "apps", http.StatusOK, tiles)
}

// setLocale cookie-sets the chosen language and redirects back. GET is
// deliberate -- the toggle is a link whose only effect is the user's own
// cookie. Open to everyone (each user toggles their own preference).
func (s *server) setLocale(w http.ResponseWriter, r *http.Request) {
	target := r.PathValue("lang")
	if !supported(i18n.SupportedLocales, target) {
		target = "en"
	}
	http.SetCookie(w, &http.Cookie{
		Name:     i18n.CookieName,
		Value:    target,
		MaxAge:   60 * 60 * 24 * 365,
		SameSite: http.SameSiteLaxMode,
		Path:     "/",
	})
	http.Redirect(w, r, safeBack(r.URL.Query().Get("back")), http.StatusSeeOther)
}

// setTheme sets light/dark/system directly, or cycles when name == "cycle".
func (s *server) setTheme(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	var target string
	switch {
	case name == "cycle":
		target = theme.Next(themeFrom(r))
	case supported(theme.SupportedThemes, name):
		target = name
	default:
		target = "system"
	}
	http.SetCookie(w, &http.Cookie{
		Name:     theme.CookieName,
		Value:    target,
		MaxAge:   60 * 60 * 24 * 365,
		SameSite: http.SameSiteLaxMode,
		Path:     "/",
	})
	http.Redirect(w, r, safeBack(r.URL.Query().Get("back")), http.StatusSeeOther)
}

func supported(set []string, v string) bool {
	for _, x := range set {
		if x == v {
			return true
		}
	}
	return false
}

// safeBack guards the ?back= redirect against open-redirect: only relative
// paths starting with a single "/" (not "//", which a browser may treat as
// protocol-relative) and carrying no scheme/host are honored.
func safeBack(value string) string {
	if value == "" || !strings.HasPrefix(value, "/") || strings.HasPrefix(value, "//") {
		return "/"
	}
	parsed, err := url.Parse(value)
	if err != nil || parsed.Scheme != "" || parsed.Host != "" {
		return "/"
	}
	return value
}

// GlobalsFromEnv builds the standard template globals from the environment,
// matching the Python main.py: the clients-tab gate plus the admin-only
// "Edit in <service>" deep-link URLs. Empty host => empty URL => the template
// omits the link rather than rendering a dead one.
func GlobalsFromEnv() map[string]any {
	kcHost := strings.TrimSpace(os.Getenv("KEYCLOAK_HOSTNAME"))
	kcRealm := strings.TrimSpace(os.Getenv("KEYCLOAK_REALM"))
	if kcRealm == "" {
		kcRealm = "vps"
	}
	keycloakURL := ""
	if kcHost != "" {
		keycloakURL = fmt.Sprintf("https://%s/admin/%s/console/#/%s/groups", kcHost, kcRealm, kcRealm)
	}
	return map[string]any{
		"clients_tab_enabled": envBool("CATENA_CLIENTS_TAB_ENABLED"),
		"keycloak_url":        keycloakURL,
		"healthchecks_url":    httpsHost(os.Getenv("INFRA_HEARTBEAT_HOSTNAME")),
		"gatus_url":           httpsHost(os.Getenv("INFRA_MONITOR_HOSTNAME")),
		"beszel_url":          httpsHost(os.Getenv("INFRA_BESZEL_HOSTNAME")),
		// The admin's own compose id -- the Apps tab hides the Configure
		// button for the matching tile so an admin cannot lock themselves out.
		"self_app_id": strings.TrimSpace(os.Getenv("CATENA_ADMIN_SELF_COMPOSE_ID")),
	}
}

func httpsHost(host string) string {
	host = strings.TrimSpace(host)
	if host == "" {
		return ""
	}
	return "https://" + host
}

func envBool(name string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(name))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}
