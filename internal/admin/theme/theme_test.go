package theme

import (
	"net/http"
	"testing"
)

func TestResolve(t *testing.T) {
	t.Setenv(DefaultThemeEnv, "")
	cases := []struct {
		name  string
		build func() *http.Request
		want  string
	}{
		{"query wins over cookie", func() *http.Request {
			r, _ := http.NewRequest("GET", "/?theme=dark", nil)
			r.AddCookie(&http.Cookie{Name: CookieName, Value: "light"})
			return r
		}, "dark"},
		{"cookie", func() *http.Request {
			r, _ := http.NewRequest("GET", "/", nil)
			r.AddCookie(&http.Cookie{Name: CookieName, Value: "light"})
			return r
		}, "light"},
		{"fallback system", func() *http.Request {
			r, _ := http.NewRequest("GET", "/", nil)
			return r
		}, "system"},
		{"unsupported query ignored", func() *http.Request {
			r, _ := http.NewRequest("GET", "/?theme=neon", nil)
			return r
		}, "system"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Resolve(tc.build()); got != tc.want {
				t.Errorf("Resolve = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestResolveEnvDefault(t *testing.T) {
	t.Setenv(DefaultThemeEnv, "dark")
	r, _ := http.NewRequest("GET", "/", nil)
	if got := Resolve(r); got != "dark" {
		t.Errorf("env default = %q, want dark", got)
	}
}

func TestNextCycles(t *testing.T) {
	cases := map[string]string{
		"light":   "dark",
		"dark":    "system",
		"system":  "light",
		"unknown": "light",
	}
	for cur, want := range cases {
		if got := Next(cur); got != want {
			t.Errorf("Next(%q) = %q, want %q", cur, got, want)
		}
	}
}
