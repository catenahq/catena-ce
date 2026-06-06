package actions

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeCatalog(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "admin-actions.yml")
	body := `actions:
  - name: backup-now
    title: Backup now
    title_fr: Sauvegarder
    category: Backups
    timeout: 300
    shell: run-backup
  - name: weird
    title: Weird
    category: Nonsense
  - name: gen-recovery
    title: Generate recovery archive
    category: Recovery
    arguments:
      - name: passphrase
        type: password
`
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadAndNormalize(t *testing.T) {
	cat := Load(writeCatalog(t))
	byName := map[string]Action{}
	for _, a := range cat {
		byName[a.Name] = a
	}
	if byName["backup-now"].Category != "Backups" || byName["backup-now"].Timeout != 300 {
		t.Errorf("backup-now = %+v", byName["backup-now"])
	}
	if byName["weird"].Category != "Ops" {
		t.Errorf("unknown category should normalize to Ops, got %q", byName["weird"].Category)
	}
	if byName["gen-recovery"].Category != "Recovery" {
		t.Errorf("Recovery must be preserved, got %q", byName["gen-recovery"].Category)
	}
	if len(byName["gen-recovery"].Arguments) != 1 || byName["gen-recovery"].Arguments[0].Type != "password" {
		t.Errorf("recovery args = %+v", byName["gen-recovery"].Arguments)
	}
	if byName["backup-now"].Source != "ce" {
		t.Error("static catalog actions should be tagged source=ce")
	}
}

func TestLoadMissingFile(t *testing.T) {
	if got := Load("/nonexistent/admin-actions.yml"); got != nil {
		t.Errorf("missing file should yield nil, got %v", got)
	}
}

func TestForTabsAndGrouping(t *testing.T) {
	cat := Load(writeCatalog(t))
	if got := len(ForRecoveryTab(cat)); got != 1 {
		t.Errorf("recovery tab actions = %d, want 1", got)
	}
	actionsTab := ForActionsTab(cat)
	if len(actionsTab) != 2 {
		t.Errorf("actions tab = %d, want 2 (recovery excluded)", len(actionsTab))
	}
	groups := GroupByCategory(actionsTab)
	if len(groups) != len(ActionsTabCategories) {
		t.Fatalf("groups = %d, want %d (stable order, empty kept)", len(groups), len(ActionsTabCategories))
	}
	if groups[0].Category != "Upgrades" || groups[1].Category != "Backups" {
		t.Errorf("category order drifted: %q %q", groups[0].Category, groups[1].Category)
	}
	// backup-now lands in Backups; weird in Ops.
	var backups, ops int
	for _, g := range groups {
		if g.Category == "Backups" {
			backups = len(g.Actions)
		}
		if g.Category == "Ops" {
			ops = len(g.Actions)
		}
	}
	if backups != 1 || ops != 1 {
		t.Errorf("Backups=%d Ops=%d, want 1/1", backups, ops)
	}
}

func TestLocalizedTitleAndSlug(t *testing.T) {
	a := Action{Title: "Backup now", TitleFR: "Sauvegarder", Category: "Initial apps setup"}
	if a.LocalizedTitle("fr") != "Sauvegarder" || a.LocalizedTitle("en") != "Backup now" {
		t.Error("localized title fallback wrong")
	}
	if a.CategorySlug() != "initial-apps-setup" {
		t.Errorf("slug = %q", a.CategorySlug())
	}
}

func TestMergedCatalogSortsCEAndPlugin(t *testing.T) {
	ce := []Action{{Name: "backup-now", Category: "Backups", Source: "ce"}}
	plugin := []Action{{Name: "audit-export", Category: "Backups", Source: "audit"}}
	merged := MergedCatalog(ce, plugin)
	if len(merged) != 2 {
		t.Fatalf("merged = %d, want 2", len(merged))
	}
	// Same category -> sorted by name: audit-export before backup-now.
	if merged[0].Name != "audit-export" {
		t.Errorf("merge order = %q first, want audit-export", merged[0].Name)
	}
}

func TestJobRegistryOneShot(t *testing.T) {
	r := NewJobRegistry()
	j := r.Create(Job{ActionName: "backup-now", Email: "op@x"})
	if j.ID == "" {
		t.Fatal("Create must assign an id")
	}
	got, ok := r.Pop(j.ID)
	if !ok || got.ActionName != "backup-now" {
		t.Fatalf("Pop = %+v ok=%v", got, ok)
	}
	if _, ok := r.Pop(j.ID); ok {
		t.Error("second Pop of the same id must return ok=false (one-shot)")
	}
}

func TestJobRegistryTTLExpiry(t *testing.T) {
	base := time.Unix(1000, 0)
	r := NewJobRegistry()
	r.now = func() time.Time { return base }
	j := r.Create(Job{ActionName: "x"})
	if r.Len() != 1 {
		t.Fatalf("len = %d, want 1", r.Len())
	}
	// Advance past the TTL; the job should sweep.
	r.now = func() time.Time { return base.Add(jobTTL + time.Second) }
	if r.Len() != 0 {
		t.Errorf("expired job should sweep, len = %d", r.Len())
	}
	if _, ok := r.Pop(j.ID); ok {
		t.Error("expired job must not Pop")
	}
}

func TestSSEFormatting(t *testing.T) {
	if got := FormatStdout("hello"); got != "data: hello\n\n" {
		t.Errorf("stdout = %q", got)
	}
	if got := FormatStderr("boom"); got != "event: stderr\ndata: boom\n\n" {
		t.Errorf("stderr = %q", got)
	}
	if got := FormatEnd(0, ""); got != "event: end\ndata: 0\n\n" {
		t.Errorf("end = %q", got)
	}
	end := FormatEnd(-1, "ssh failed")
	if !strings.Contains(end, "data: -1") || !strings.Contains(end, "data: error: ssh failed") {
		t.Errorf("end with error = %q", end)
	}
	if got := FormatStart("backup-now", "op@x"); !strings.Contains(got, "data: action=backup-now email=op@x") {
		t.Errorf("start = %q", got)
	}
}

func TestBuildCommand(t *testing.T) {
	if got := BuildCommand("backup-now", ""); got != "backup-now" {
		t.Errorf("no payload = %q", got)
	}
	if got := BuildCommand("gen-recovery", "secret-pass"); got != "gen-recovery secret-pass" {
		t.Errorf("with payload = %q", got)
	}
}
