// Package labels parses the vps.* compose-label vocabulary the catena-admin
// Apps/Access tabs read, ported from the Python catena_admin.labels.schema.
// It is the source of truth for vps.auth.* (per-app gating intent),
// vps.homepage.* (tile presentation), default-deny auth-mode resolution, and
// image-tag classification for managed-update eligibility.
package labels

import (
	"regexp"
	"sort"
	"strings"
)

// ─── Slugification ──────────────────────────────────────────────────────

var slugRe = regexp.MustCompile(`[^a-z0-9-]+`)

// Slugify lowercases, replaces non-[a-z0-9-] runs with "-", and trims dashes.
// Used for Traefik router/service names and Dokploy compose alias matching.
func Slugify(s string) string {
	return strings.Trim(slugRe.ReplaceAllString(strings.ToLower(s), "-"), "-")
}

// ─── vps.auth.* extraction ──────────────────────────────────────────────

var authLabelRe = regexp.MustCompile(
	`(?i)['"]?vps\.auth\.(groups|mode|protected|oidc(?:\.redirect_uris|\.scopes)?)['"]?` +
		`\s*[=:]\s*['"]?([^'"\n#]+?)['"]?\s*(?:\n|$|#)`)

// AuthLabels holds the parsed vps.auth.* labels. Absent labels leave zero
// values (Mode "", Groups nil), which ResolveAuthMode treats as unset.
type AuthLabels struct {
	Groups           []string
	Mode             string
	Protected        bool
	OIDC             bool
	OIDCRedirectURIs []string
	OIDCScopes       []string
}

func truthy(v string) bool {
	switch strings.ToLower(v) {
	case "true", "yes", "1", "on":
		return true
	}
	return false
}

// ExtractAuthLabels pulls the vps.auth.* labels from compose text.
func ExtractAuthLabels(composeText string) AuthLabels {
	var out AuthLabels
	if composeText == "" {
		return out
	}
	for _, m := range authLabelRe.FindAllStringSubmatch(composeText, -1) {
		key := strings.ToLower(m[1])
		val := strings.TrimSpace(m[2])
		switch key {
		case "groups":
			out.Groups = splitCSV(val)
		case "mode":
			out.Mode = strings.ToLower(val)
		case "protected":
			out.Protected = truthy(val)
		case "oidc":
			out.OIDC = truthy(val)
		case "oidc.redirect_uris":
			out.OIDCRedirectURIs = splitCSV(val)
		case "oidc.scopes":
			out.OIDCScopes = strings.Fields(val)
		}
	}
	return out
}

// ─── vps.homepage.* extraction ──────────────────────────────────────────

var homepageLabelRe = regexp.MustCompile(
	`(?i)['"]?vps\.homepage\.(name|icon|description|hidden)['"]?` +
		`\s*[=:]\s*['"]?([^'"\n#]+?)['"]?\s*(?:\n|$|#)`)

// HomepageLabels holds the parsed vps.homepage.* labels (tile presentation).
type HomepageLabels struct {
	Name        string
	Icon        string
	Description string
	Hidden      bool
}

// ExtractHomepageLabels pulls the vps.homepage.* labels from compose text.
func ExtractHomepageLabels(composeText string) HomepageLabels {
	var out HomepageLabels
	if composeText == "" {
		return out
	}
	for _, m := range homepageLabelRe.FindAllStringSubmatch(composeText, -1) {
		key := strings.ToLower(m[1])
		val := strings.TrimSpace(m[2])
		switch key {
		case "name":
			out.Name = val
		case "icon":
			out.Icon = val
		case "description":
			out.Description = val
		case "hidden":
			out.Hidden = truthy(val)
		}
	}
	return out
}

// ─── Auth-mode resolution (default-deny) ────────────────────────────────

// AuthMode is the normalized resolved mode.
type AuthMode = string

// ResolveAuthMode collapses vps.auth.mode + vps.auth.groups into
// (resolvedMode, allowedGroups, isPublic) under DEFAULT-DENY, returning any
// advisory warnings. admin is always present in a non-public allowed set so
// the operator can never be label-locked out. resolvedMode is one of
// public | admin-only | restricted | deny.
func ResolveAuthMode(l AuthLabels, appName string) (mode AuthMode, allowed []string, isPublic bool, warnings []string) {
	rawMode := strings.ToLower(strings.TrimSpace(l.Mode))
	var groups []string
	for _, g := range l.Groups {
		if g != "" {
			groups = append(groups, g)
		}
	}
	hasVisitor := contains(groups, "visitor")

	switch {
	// 1. visitor keyword / explicit public -> public (no auth).
	case rawMode == "public" || hasVisitor:
		if rawMode == "public" && hasNonVisitor(groups) {
			warnings = append(warnings, "["+appName+"]: vps.auth.mode=public ignores vps.auth.groups; no gating will be applied. Drop one of the labels to clarify.")
		}
		if hasVisitor && distinctCount(groups) > 1 {
			warnings = append(warnings, "["+appName+"]: vps.auth.groups mixes `visitor` with other groups; `visitor` means public, so the others are ignored. Drop `visitor` to gate.")
		}
		return "public", nil, true, warnings
	// 2. admin-only sugar.
	case rawMode == "admin-only":
		if len(groups) > 0 && !onlyAdmin(groups) {
			warnings = append(warnings, "["+appName+"]: vps.auth.mode=admin-only overrides vps.auth.groups; using ['admin']. Drop one of the labels to clarify.")
		}
		return "admin-only", []string{"admin"}, false, warnings
	// 3. private: gated to the listed groups; broad authenticated default.
	case rawMode == "private":
		if len(groups) > 0 {
			return "restricted", sortedUnion(groups, "admin"), false, warnings
		}
		return "restricted", []string{"admin", "client", "staff"}, false, warnings
	// 4. explicit groups, no/blank mode -> per-group default-deny.
	case len(groups) > 0:
		if rawMode != "" {
			warnings = append(warnings, "["+appName+"]: unknown vps.auth.mode="+rawMode+"; honoring vps.auth.groups. Valid modes: public, private, admin-only.")
		}
		return "restricted", sortedUnion(groups, "admin"), false, warnings
	// 5. nothing declared (or unknown mode + no groups) -> DENY.
	default:
		if rawMode != "" {
			warnings = append(warnings, "["+appName+"]: unknown vps.auth.mode="+rawMode+" and no vps.auth.groups; defaulting to DENY (admin-only).")
		} else {
			warnings = append(warnings, "["+appName+"]: no vps.auth.mode or vps.auth.groups label; defaulting to DENY (admin-only). Add vps.auth.groups=... or vps.auth.mode=public.")
		}
		return "deny", []string{"admin"}, false, warnings
	}
}

// ─── Image-tag classification (managed-update eligibility) ──────────────

var (
	fullSemverRe    = regexp.MustCompile(`^v?\d+\.\d+\.\d+(?:[.-][\w.-]+)?$`)
	partialSemverRe = regexp.MustCompile(`^v?\d+(?:\.\d+)?(?:[.-][\w.-]+)?$`)
)

var floatingTags = map[string]struct{}{
	"latest": {}, "stable": {}, "alpine": {}, "edge": {}, "main": {}, "master": {},
}

// ClassifyImageTag returns (class, tag): full_semver (managed-update eligible),
// partial (X or X.Y only), floating (latest/branch/etc), or unset (no tag).
func ClassifyImageTag(image string) (class, tag string) {
	if image == "" {
		return "unset", ""
	}
	bare := strings.SplitN(image, "@", 2)[0]
	if !strings.Contains(bare, ":") {
		return "unset", ""
	}
	parts := strings.Split(bare, ":")
	tag = strings.TrimSpace(parts[len(parts)-1])
	if tag == "" {
		return "unset", ""
	}
	if _, ok := floatingTags[strings.ToLower(tag)]; ok {
		return "floating", tag
	}
	if fullSemverRe.MatchString(tag) {
		return "full_semver", tag
	}
	if partialSemverRe.MatchString(tag) {
		return "partial", tag
	}
	return "floating", tag
}

// ─── helpers ────────────────────────────────────────────────────────────

func splitCSV(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func contains(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}

func hasNonVisitor(groups []string) bool {
	for _, g := range groups {
		if g != "visitor" {
			return true
		}
	}
	return false
}

func distinctCount(groups []string) int {
	seen := map[string]struct{}{}
	for _, g := range groups {
		seen[g] = struct{}{}
	}
	return len(seen)
}

func onlyAdmin(groups []string) bool {
	for _, g := range groups {
		if g != "admin" {
			return false
		}
	}
	return len(groups) > 0
}

// sortedUnion returns the deduped, sorted union of groups + extra.
func sortedUnion(groups []string, extra string) []string {
	seen := map[string]struct{}{extra: {}}
	for _, g := range groups {
		seen[g] = struct{}{}
	}
	out := make([]string, 0, len(seen))
	for g := range seen {
		out = append(out, g)
	}
	sort.Strings(out)
	return out
}
