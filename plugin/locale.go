package plugin

import "context"

// localeKey carries the request locale across the plugin Render boundary.
// It is an additive, backwards-compatible channel: the shell stamps the
// resolved locale onto the context before calling Render, and plugins that
// care read it via LocaleFromContext. Plugins that ignore it (the original
// English-only panels) are unaffected. This avoids changing the Render
// signature ahead of the full request/response transport slice.
type localeKey struct{}

// ContextWithLocale returns ctx carrying locale, for the shell to set before
// invoking a plugin's Render.
func ContextWithLocale(ctx context.Context, locale string) context.Context {
	return context.WithValue(ctx, localeKey{}, locale)
}

// LocaleFromContext returns the request locale a plugin should render in,
// defaulting to "en" when the shell did not set one (older shell, or a
// non-request call path).
func LocaleFromContext(ctx context.Context) string {
	if v, ok := ctx.Value(localeKey{}).(string); ok && v != "" {
		return v
	}
	return "en"
}
