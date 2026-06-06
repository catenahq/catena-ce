// Package apps assembles the Apps-tab tile grid from Dokploy (app/compose
// list + domains + compose body), the vps.* labels, and live Gatus status,
// plus optional operator-authored extra tiles. Read-only assembly + per-user
// visibility filtering; the Configure-side mutations live elsewhere. Ported
// from the Python catena_admin.apps.
package apps

import (
	"os"
	"regexp"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/catenahq/catena-ce/internal/admin/auth"
	"github.com/catenahq/catena-ce/internal/admin/integrations"
	"github.com/catenahq/catena-ce/internal/admin/labels"
)

const defaultIcon = "mdi-application"

// DokployLister + GatusByHost are the read surfaces BuildTiles needs; the
// concrete integration clients satisfy them.
type DokployLister interface {
	ListItems(forceRefresh bool) []integrations.DokployItem
}

type GatusByHost interface {
	GetStatusByHost(host string) (integrations.EndpointStatus, bool)
}

// Tile is one renderable Apps-grid entry. DokployID is the composeId /
// applicationId (empty for operator extras). Kind is application | compose |
// extra (only compose supports the Configure form).
type Tile struct {
	DokployID     string
	Slug          string
	Kind          string
	Name          string
	NameFR        string
	Description   string
	DescriptionFR string
	Icon          string
	URL           string
	Mode          string // public | admin-only | restricted | deny
	Groups        []string
	Hidden        bool
	OIDC          bool
	AutoUpdate    string
	Health        string // healthy | unhealthy | unknown
	LastCheckTS   *float64
	RawAppName    string
	ProjectName   string
	ComposeBody   string
	Badges        []string
	Protected     bool
}

// LocalizedName returns the tile name for locale, falling back to EN.
func (t Tile) LocalizedName(locale string) string {
	if locale == "fr" && t.NameFR != "" {
		return t.NameFR
	}
	return t.Name
}

// LocalizedDescription returns the tile description for locale, falling back to EN.
func (t Tile) LocalizedDescription(locale string) string {
	if locale == "fr" && t.DescriptionFR != "" {
		return t.DescriptionFR
	}
	return t.Description
}

// BuildTiles assembles the per-user tile list with visibility already applied:
// staff see public tiles + tiles whose groups intersect theirs (hidden
// excluded); admins see everything (badged).
func BuildTiles(dokploy DokployLister, gatus GatusByHost, identity auth.Identity, extraTilesPath string) []Tile {
	var candidates []Tile
	candidates = append(candidates, dokployTiles(dokploy, gatus)...)
	candidates = append(candidates, extraTiles(extraTilesPath, gatus)...)

	isAdmin := identity.IsAdmin()
	out := candidates[:0]
	for _, t := range candidates {
		if isVisibleTo(t, identity, isAdmin) {
			out = append(out, t)
		}
	}
	return out
}

var autoUpdateRe = regexp.MustCompile(`(?i)['"]?vps\.auto-update['"]?\s*[=:]\s*['"]?([a-zA-Z+]+)`)

func extractUpdatePolicy(composeBody string) string {
	m := autoUpdateRe.FindStringSubmatch(composeBody)
	if m == nil {
		return "unset"
	}
	return strings.ToLower(m[1])
}

func healthFromStatus(status integrations.EndpointStatus, ok bool) (string, *float64) {
	if !ok || status.Healthy == nil {
		return "unknown", nil
	}
	if *status.Healthy {
		return "healthy", status.LastCheckTS
	}
	return "unhealthy", status.LastCheckTS
}

func dokployTiles(dokploy DokployLister, gatus GatusByHost) []Tile {
	if dokploy == nil {
		return nil
	}
	var out []Tile
	for _, item := range dokploy.ListItems(false) {
		if len(item.Domains) == 0 || item.Domains[0].Host == "" {
			continue // no domain -> not reachable; skip the tile
		}
		primaryHost := item.Domains[0].Host
		authLabels := labels.ExtractAuthLabels(item.ComposeBody)
		hpLabels := labels.ExtractHomepageLabels(item.ComposeBody)
		mode, resolvedGroups, _, _ := labels.ResolveAuthMode(authLabels, item.AppName)
		var status integrations.EndpointStatus
		var ok bool
		if gatus != nil {
			status, ok = gatus.GetStatusByHost(primaryHost)
		}
		health, ts := healthFromStatus(status, ok)

		var badges []string
		if hpLabels.Hidden {
			badges = append(badges, "hidden")
		}
		switch mode {
		case "admin-only":
			badges = append(badges, "admin-only")
		case "deny":
			badges = append(badges, "deny")
		case "public":
			badges = append(badges, "public")
		}
		if authLabels.OIDC {
			badges = append(badges, "oidc")
		}

		slug := labels.Slugify(item.AppName)
		if slug == "" {
			slug = item.ItemID
		}
		out = append(out, Tile{
			DokployID:   item.ItemID,
			Slug:        slug,
			Kind:        item.Kind,
			Name:        firstNonEmpty(hpLabels.Name, item.AppName),
			Description: firstNonEmpty(hpLabels.Description, item.Description),
			Icon:        firstNonEmpty(hpLabels.Icon, defaultIcon),
			URL:         "https://" + primaryHost,
			Mode:        mode,
			Groups:      resolvedGroups,
			Hidden:      hpLabels.Hidden,
			OIDC:        authLabels.OIDC,
			AutoUpdate:  extractUpdatePolicy(item.ComposeBody),
			Health:      health,
			LastCheckTS: ts,
			RawAppName:  item.AppName,
			ProjectName: item.ProjectName,
			ComposeBody: item.ComposeBody,
			Badges:      badges,
			Protected:   authLabels.Protected,
		})
	}
	return out
}

type extraTileFile struct {
	Tiles []struct {
		ID            string   `yaml:"id"`
		Name          string   `yaml:"name"`
		NameFR        string   `yaml:"name_fr"`
		Description   string   `yaml:"description"`
		DescriptionFR string   `yaml:"description_fr"`
		Icon          string   `yaml:"icon"`
		URL           string   `yaml:"url"`
		Mode          string   `yaml:"mode"`
		Groups        []string `yaml:"groups"`
		Hidden        bool     `yaml:"hidden"`
	} `yaml:"tiles"`
}

// extraTiles reads operator-authored tiles from extra-tiles.yml (rendered from
// inventory by the Ansible role). Missing/invalid file -> no extras.
func extraTiles(path string, gatus GatusByHost) []Tile {
	if path == "" {
		path = envOr("CATENA_ADMIN_EXTRA_TILES", "/etc/catena/extra-tiles.yml")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var data extraTileFile
	if err := yaml.Unmarshal(raw, &data); err != nil {
		return nil
	}
	var out []Tile
	for _, e := range data.Tiles {
		host := hostOf(e.URL)
		var status integrations.EndpointStatus
		var ok bool
		if host != "" && gatus != nil {
			status, ok = gatus.GetStatusByHost(host)
		}
		health, ts := healthFromStatus(status, ok)

		mode := strings.ToLower(e.Mode)
		if mode == "" {
			mode = "private"
		}
		groups := e.Groups
		switch {
		case mode == "admin-only":
			groups = []string{auth.AdminGroup}
		case mode == "public":
			groups = nil
		case len(groups) == 0:
			groups = []string{auth.StaffGroup}
		}
		slug := e.ID
		if slug == "" {
			slug = labels.Slugify(e.Name)
		}
		out = append(out, Tile{
			Slug:          slug,
			Kind:          "extra",
			Name:          e.Name,
			NameFR:        e.NameFR,
			Description:   e.Description,
			DescriptionFR: e.DescriptionFR,
			Icon:          firstNonEmpty(e.Icon, defaultIcon),
			URL:           e.URL,
			Mode:          mode,
			Groups:        groups,
			Hidden:        e.Hidden,
			AutoUpdate:    "unset",
			Health:        health,
			LastCheckTS:   ts,
			Badges:        []string{"extra"},
		})
	}
	return out
}

func isVisibleTo(tile Tile, identity auth.Identity, isAdmin bool) bool {
	if isAdmin {
		return true // admins see everything, hidden + admin-only included
	}
	if tile.Hidden {
		return false
	}
	switch tile.Mode {
	case "public":
		return true
	case "admin-only":
		return false
	}
	// restricted / deny: intersect the user's groups with the tile's allowed
	// groups. deny tiles resolve to allowed=[admin] only, so a non-admin never
	// matches (default-deny).
	for _, g := range tile.Groups {
		if identity.HasGroup(g) {
			return true
		}
	}
	return false
}

func hostOf(url string) string {
	if url == "" {
		return ""
	}
	if i := strings.Index(url, "://"); i >= 0 {
		url = url[i+3:]
	}
	url = strings.SplitN(url, "/", 2)[0]
	return strings.SplitN(url, ":", 2)[0]
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

func envOr(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}
