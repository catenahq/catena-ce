package web

import (
	"context"
	"errors"
	"net/http"
	"os"
	"strings"
	"time"

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

// NativeAuth is the direct (tailnet) listener's identity middleware. Unlike
// RequestState it never reads the X-Forwarded-* identity headers -- a tailnet
// peer could forge them -- so trust is decided by which socket received the
// request (the host port only reaches this listener; oauth2-proxy only reaches
// the internal one). Identity comes solely from a valid signed session cookie.
// Unauthenticated: an HTML GET is redirected to /login, everything else gets
// 401. Static assets + the health probe are always open (the login page needs
// its CSS; the container healthcheck must not require a session).
func NativeAuth(codec *auth.SessionCodec, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/_/static/") || r.URL.Path == "/health" {
			next.ServeHTTP(w, r)
			return
		}
		id, ok := auth.IdentityFromSession(codec, r, time.Now())
		if !ok {
			if r.Method == http.MethodGet {
				http.Redirect(w, r, "/login", http.StatusSeeOther)
				return
			}
			http.Error(w, "Sign-in required.", http.StatusUnauthorized)
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

// RequireAuthenticated wraps a handler open to any signed-in user (admin,
// staff, or client) but closed to anonymous visitors. Used by the
// client-facing surfaces (dashboard) that are not admin-only.
func RequireAuthenticated(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !identityFrom(r).IsAuthenticated() {
			http.Error(w, "Sign-in required.", http.StatusForbidden)
			return
		}
		h(w, r)
	}
}

// RequireGroups wraps a handler reachable only by an identity in at least
// one of the named groups (admin always passes). A non-matching identity
// gets 403 before the handler runs.
func RequireGroups(h http.HandlerFunc, groups ...string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id := identityFrom(r)
		if !id.IsAdmin() && !anyGroup(id, groups) {
			http.Error(w, "Group membership required.", http.StatusForbidden)
			return
		}
		h(w, r)
	}
}

func anyGroup(id auth.Identity, groups []string) bool {
	for _, g := range groups {
		if id.HasGroup(g) {
			return true
		}
	}
	return false
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
