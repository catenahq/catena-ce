// Package theme is the catena-admin shell's light/dark/system resolution,
// ported from the Python catena_admin.theme. The body gets a theme-{name}
// class; CSS defines both palettes and .theme-system falls back to
// prefers-color-scheme, so the default needs no JS.
package theme

import (
	"net/http"
	"os"
	"strings"
)

const (
	CookieName      = "catena_theme"
	QueryParam      = "theme"
	DefaultThemeEnv = "CATENA_DEFAULT_THEME"
)

// SupportedThemes is the cycle order for the header toggle.
var SupportedThemes = []string{"light", "dark", "system"}

func isSupported(name string) bool {
	for _, t := range SupportedThemes {
		if t == name {
			return true
		}
	}
	return false
}

// Resolve picks the request theme by precedence: ?theme query, then the
// catena_theme cookie, then CATENA_DEFAULT_THEME, then "system".
func Resolve(r *http.Request) string {
	if qp := r.URL.Query().Get(QueryParam); isSupported(qp) {
		return qp
	}
	if c, err := r.Cookie(CookieName); err == nil && isSupported(c.Value) {
		return c.Value
	}
	if env := strings.ToLower(strings.TrimSpace(os.Getenv(DefaultThemeEnv))); isSupported(env) {
		return env
	}
	return "system"
}

// Next cycles light -> dark -> system -> light for the header toggle. An
// unknown current value resets to the first theme.
func Next(current string) string {
	for i, t := range SupportedThemes {
		if t == current {
			return SupportedThemes[(i+1)%len(SupportedThemes)]
		}
	}
	return SupportedThemes[0]
}
