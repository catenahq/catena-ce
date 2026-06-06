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
