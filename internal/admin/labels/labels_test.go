package labels

import (
	"reflect"
	"testing"
)

func TestSlugify(t *testing.T) {
	cases := map[string]string{
		"Invoice Ninja":  "invoice-ninja",
		"  Foo_Bar!! ":   "foo-bar",
		"already-slug":   "already-slug",
		"CAPS":           "caps",
		"a/b:c":          "a-b-c",
	}
	for in, want := range cases {
		if got := Slugify(in); got != want {
			t.Errorf("Slugify(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestExtractAuthLabels(t *testing.T) {
	compose := `
services:
  app:
    labels:
      - "vps.auth.mode=private"
      - "vps.auth.groups=staff, accounting"
      - "vps.auth.protected=true"
      - "vps.auth.oidc=yes"
      - "vps.auth.oidc.scopes=openid profile email"
`
	got := ExtractAuthLabels(compose)
	if got.Mode != "private" {
		t.Errorf("Mode = %q, want private", got.Mode)
	}
	if !reflect.DeepEqual(got.Groups, []string{"staff", "accounting"}) {
		t.Errorf("Groups = %v, want [staff accounting]", got.Groups)
	}
	if !got.Protected || !got.OIDC {
		t.Errorf("Protected/OIDC = %v/%v, want true/true", got.Protected, got.OIDC)
	}
	if !reflect.DeepEqual(got.OIDCScopes, []string{"openid", "profile", "email"}) {
		t.Errorf("OIDCScopes = %v", got.OIDCScopes)
	}
}

func TestExtractHomepageLabels(t *testing.T) {
	compose := `labels:
  - "vps.homepage.name=Invoice Ninja"
  - "vps.homepage.icon=mdi-invoice"
  - "vps.homepage.hidden=true"`
	got := ExtractHomepageLabels(compose)
	if got.Name != "Invoice Ninja" || got.Icon != "mdi-invoice" || !got.Hidden {
		t.Errorf("got %+v", got)
	}
}

func TestResolveAuthMode(t *testing.T) {
	cases := []struct {
		name      string
		labels    AuthLabels
		wantMode  string
		wantGroup []string
		wantPub   bool
	}{
		{"public mode", AuthLabels{Mode: "public"}, "public", nil, true},
		{"visitor keyword", AuthLabels{Groups: []string{"visitor"}}, "public", nil, true},
		{"admin-only", AuthLabels{Mode: "admin-only"}, "admin-only", []string{"admin"}, false},
		{"private + groups", AuthLabels{Mode: "private", Groups: []string{"staff"}}, "restricted", []string{"admin", "staff"}, false},
		{"private no groups", AuthLabels{Mode: "private"}, "restricted", []string{"admin", "client", "staff"}, false},
		{"groups no mode", AuthLabels{Groups: []string{"accounting"}}, "restricted", []string{"accounting", "admin"}, false},
		{"nothing -> deny", AuthLabels{}, "deny", []string{"admin"}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			mode, allowed, pub, _ := ResolveAuthMode(tc.labels, "app")
			if mode != tc.wantMode || pub != tc.wantPub {
				t.Errorf("mode/pub = %q/%v, want %q/%v", mode, pub, tc.wantMode, tc.wantPub)
			}
			if !reflect.DeepEqual(allowed, tc.wantGroup) {
				t.Errorf("allowed = %v, want %v", allowed, tc.wantGroup)
			}
		})
	}
}

func TestResolveAuthModeWarns(t *testing.T) {
	_, _, _, w := ResolveAuthMode(AuthLabels{Mode: "public", Groups: []string{"staff"}}, "app")
	if len(w) == 0 {
		t.Error("expected a warning for public + groups")
	}
	_, _, _, w = ResolveAuthMode(AuthLabels{}, "app")
	if len(w) == 0 {
		t.Error("expected a deny warning for no labels")
	}
}

func TestClassifyImageTag(t *testing.T) {
	cases := []struct {
		image     string
		wantClass string
		wantTag   string
	}{
		{"app:1.2.3", "full_semver", "1.2.3"},
		{"app:v1.2.3", "full_semver", "v1.2.3"},
		{"app:1.2", "partial", "1.2"},
		{"app:1", "partial", "1"},
		{"app:latest", "floating", "latest"},
		{"app:main", "floating", "main"},
		{"app:weird-tag", "floating", "weird-tag"},
		{"app", "unset", ""},
		{"app@sha256:abc", "unset", ""},
		{"app:1.2.3@sha256:abc", "full_semver", "1.2.3"},
		{"", "unset", ""},
	}
	for _, tc := range cases {
		class, tag := ClassifyImageTag(tc.image)
		if class != tc.wantClass || tag != tc.wantTag {
			t.Errorf("ClassifyImageTag(%q) = %q/%q, want %q/%q", tc.image, class, tag, tc.wantClass, tc.wantTag)
		}
	}
}
