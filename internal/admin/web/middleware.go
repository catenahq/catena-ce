package web

import (
	"context"
	"errors"
	"net/http"
	"os"
	"strings"

	"github.com/catenahq/catena-ce/internal/admin/auth"
	"github.com/catenahq/catena-ce/internal/admin/i18n"
	"github.com/catenahq/catena-ce/internal/admin/theme"
)

type ctxKey int

const (
	localeKey ctxKey = iota
	themeKey
	identityKey
)

// RequestState resolves locale, theme, and identity once per request and
// stashes them on the context so templates + routes read them without
// re-resolving. A header-signature failure (when the operator requires it)
// is rejected with 403 before any handler runs.
func RequestState(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id, err := auth.IdentityFromRequest(r)
		if err != nil {
			status := http.StatusForbidden
			if errors.Is(err, auth.ErrMissingSig) || errors.Is(err, auth.ErrSigMismatch) {
				http.Error(w, err.Error(), status)
				return
			}
			http.Error(w, err.Error(), status)
			return
		}
		ctx := context.WithValue(r.Context(), localeKey, i18n.ResolveLocale(r))
		ctx = context.WithValue(ctx, themeKey, theme.Resolve(r))
		ctx = context.WithValue(ctx, identityKey, id)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func localeFrom(r *http.Request) string {
	if v, ok := r.Context().Value(localeKey).(string); ok {
		return v
	}
	return i18n.ResolveLocale(r)
}

func themeFrom(r *http.Request) string {
	if v, ok := r.Context().Value(themeKey).(string); ok {
		return v
	}
	return theme.Resolve(r)
}

func identityFrom(r *http.Request) auth.Identity {
	if v, ok := r.Context().Value(identityKey).(auth.Identity); ok {
		return v
	}
	id, _ := auth.IdentityFromRequest(r)
	return id
}

// RequireAdmin wraps an admin-only handler: a non-admin identity gets 403
// before the handler runs. Every mutating + admin route is wrapped; public
// routes are not. (The Python enforced this via a route-introspection test;
// here the wrapper is the single explicit gate.)
func RequireAdmin(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !identityFrom(r).IsAdmin() {
			http.Error(w, "Admin group membership required.", http.StatusForbidden)
			return
		}
		h(w, r)
	}
}

// defaultCSP is strict same-origin plus the one external script source the
// base layout needs (htmx + htmx-ext-sse on unpkg). Operators override
// verbatim via CATENA_ADMIN_CSP (a typo turns the header off rather than
// silently widening it). HSTS is intentionally absent -- TLS terminates at
// the Cloudflare Tunnel edge, so HSTS belongs there.
const defaultCSP = "default-src 'self'; " +
	"script-src 'self' https://unpkg.com; " +
	"style-src 'self'; " +
	"img-src 'self' data:; " +
	"connect-src 'self'; " +
	"frame-ancestors 'none'; " +
	"base-uri 'self'; " +
	"form-action 'self'"

// SecurityHeaders sets origin-policy headers on every response, each only
// when a downstream handler has not already set its own (setdefault
// semantics, matching the Python middleware).
func SecurityHeaders(next http.Handler) http.Handler {
	csp := strings.TrimSpace(os.Getenv("CATENA_ADMIN_CSP"))
	if csp == "" {
		csp = defaultCSP
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		setDefault(h, "Content-Security-Policy", csp)
		setDefault(h, "X-Frame-Options", "DENY")
		setDefault(h, "X-Content-Type-Options", "nosniff")
		setDefault(h, "Referrer-Policy", "same-origin")
		next.ServeHTTP(w, r)
	})
}

func setDefault(h http.Header, key, value string) {
	if h.Get(key) == "" {
		h.Set(key, value)
	}
}
