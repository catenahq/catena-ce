package apps

import (
	"testing"

	"github.com/catenahq/catena-ce/internal/admin/auth"
	"github.com/catenahq/catena-ce/internal/admin/integrations"
)

type fakeDokploy struct{ items []integrations.DokployItem }

func (f fakeDokploy) ListItems(bool) []integrations.DokployItem { return f.items }

type fakeGatus struct{ byHost map[string]integrations.EndpointStatus }

func (f fakeGatus) GetStatusByHost(h string) (integrations.EndpointStatus, bool) {
	s, ok := f.byHost[h]
	return s, ok
}

func boolp(b bool) *bool { return &b }

func items() []integrations.DokployItem {
	return []integrations.DokployItem{
		{ // public app, healthy
			ProjectName: "client", Kind: "compose", ItemID: "c1", AppName: "Nextcloud",
			Domains: []integrations.Domain{{Host: "cloud.example.com"}},
			ComposeBody: `    labels:
      - "vps.auth.mode=public"
      - "vps.homepage.name=Cloud"
`,
		},
		{ // staff-gated
			ProjectName: "client", Kind: "compose", ItemID: "c2", AppName: "Kimai",
			Domains: []integrations.Domain{{Host: "time.example.com"}},
			ComposeBody: `    labels:
      - "vps.auth.groups=staff"
`,
		},
		{ // deny (no labels) -> admin-only
			ProjectName: "client", Kind: "compose", ItemID: "c3", AppName: "Secret",
			Domains:     []integrations.Domain{{Host: "secret.example.com"}},
			ComposeBody: ``,
		},
		{ // no domain -> skipped entirely
			ProjectName: "client", Kind: "application", ItemID: "a1", AppName: "Nodomain",
		},
	}
}

func gatus() fakeGatus {
	return fakeGatus{byHost: map[string]integrations.EndpointStatus{
		"cloud.example.com": {Name: "nextcloud", Healthy: boolp(true)},
	}}
}

func TestBuildTilesStaffVisibility(t *testing.T) {
	staff := auth.Identity{Email: "s@x", Groups: []string{"staff"}}
	tiles := BuildTiles(fakeDokploy{items()}, gatus(), staff, "/nonexistent")

	bySlug := map[string]Tile{}
	for _, t := range tiles {
		bySlug[t.Slug] = t
	}
	if _, ok := bySlug["nextcloud"]; !ok {
		t.Error("staff should see the public Nextcloud tile")
	}
	if _, ok := bySlug["kimai"]; !ok {
		t.Error("staff should see the staff-gated Kimai tile")
	}
	if _, ok := bySlug["secret"]; ok {
		t.Error("staff must NOT see the deny (admin-only) Secret tile")
	}
	if _, ok := bySlug["nodomain"]; ok {
		t.Error("an item with no domain must be skipped")
	}
	// Public tile carries the homepage name + healthy status.
	nc := bySlug["nextcloud"]
	if nc.Name != "Cloud" || nc.Mode != "public" || nc.Health != "healthy" {
		t.Errorf("nextcloud tile = name %q mode %q health %q", nc.Name, nc.Mode, nc.Health)
	}
}

func TestBuildTilesAdminSeesAll(t *testing.T) {
	admin := auth.Identity{Email: "op@x", Groups: []string{"admin"}}
	tiles := BuildTiles(fakeDokploy{items()}, gatus(), admin, "/nonexistent")
	// 3 with domains (nodomain skipped); admin sees deny + everything.
	if len(tiles) != 3 {
		t.Fatalf("admin tiles = %d, want 3", len(tiles))
	}
	var sawSecret bool
	for _, tl := range tiles {
		if tl.Slug == "secret" {
			sawSecret = true
			if tl.Mode != "deny" {
				t.Errorf("Secret mode = %q, want deny", tl.Mode)
			}
		}
	}
	if !sawSecret {
		t.Error("admin must see the deny tile")
	}
}

func TestLocalizedNameFallback(t *testing.T) {
	tile := Tile{Name: "Cloud", NameFR: ""}
	if tile.LocalizedName("fr") != "Cloud" {
		t.Error("missing fr name should fall back to en")
	}
	tile.NameFR = "Nuage"
	if tile.LocalizedName("fr") != "Nuage" {
		t.Error("fr name should win for fr locale")
	}
}
