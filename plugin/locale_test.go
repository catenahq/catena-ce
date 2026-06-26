package plugin

import (
	"context"
	"testing"
)

func TestLocaleFromContextDefault(t *testing.T) {
	if got := LocaleFromContext(context.Background()); got != "en" {
		t.Errorf("unset locale = %q, want en", got)
	}
}

func TestLocaleRoundTrip(t *testing.T) {
	ctx := ContextWithLocale(context.Background(), "fr")
	if got := LocaleFromContext(ctx); got != "fr" {
		t.Errorf("locale = %q, want fr", got)
	}
}

func TestEmptyLocaleFallsBackToEn(t *testing.T) {
	ctx := ContextWithLocale(context.Background(), "")
	if got := LocaleFromContext(ctx); got != "en" {
		t.Errorf("empty locale = %q, want en fallback", got)
	}
}
