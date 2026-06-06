package web

import (
	"encoding/json"
	"fmt"
	"io/fs"
	"net/http"
	"net/url"
	"os"
	"strings"

	"github.com/catenahq/catena-ce/internal/admin/i18n"
	"github.com/catenahq/catena-ce/internal/admin/theme"
)

// Config wires the shell server. Globals are exposed to every template
// (feature gates + deep-link URLs); an empty TranslationsDir uses the
// embedded EN/FR copy (the bind-mount override path the Python app had).
type Config struct {
	Version         string
	Globals         map[string]any
	TranslationsDir string
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
	s := &server{tmpl: tmpl, version: cfg.Version}

	mux := http.NewServeMux()
	staticSub, err := fs.Sub(staticFS, "static")
	if err != nil {
		return nil, err
	}
	mux.Handle("GET /_/static/", http.StripPrefix("/_/static/", http.FileServer(http.FS(staticSub))))
	mux.HandleFunc("GET /health", s.health)
	mux.HandleFunc("GET /{$}", s.rootRedirect)
	mux.HandleFunc("GET /apps", s.apps)
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
	tmpl    *Templates
	version string
}

func (s *server) health(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok", "version": s.version})
}

// rootRedirect: / is the canonical landing for everyone -> /apps.
func (s *server) rootRedirect(w http.ResponseWriter, r *http.Request) {
	http.Redirect(w, r, "/apps", http.StatusSeeOther)
}

// apps is the landing tile grid. Placeholder until the Apps tab port lands;
// renders the welcome page for now.
func (s *server) apps(w http.ResponseWriter, r *http.Request) {
	s.tmpl.Render(w, r, "index", http.StatusOK, nil)
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
