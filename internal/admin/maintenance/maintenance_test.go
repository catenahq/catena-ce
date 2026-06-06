package maintenance

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/catenahq/catena-ce/internal/admin/i18n"
)

// testTranslations builds a tiny EN/FR store with the maintenance keys the
// assembler needs.
func testTranslations(t *testing.T) *i18n.Translations {
	t.Helper()
	dir := t.TempDir()
	en := "" +
		"maintenance:\n" +
		"  event:\n" +
		"    backup_ok: \"Backup completed at {time}. Size {size}.\"\n" +
		"    _unknown: \"Maintenance activity recorded.\"\n"
	fr := "" +
		"maintenance:\n" +
		"  event:\n" +
		"    backup_ok: \"Sauvegarde terminee a {time}. Taille {size}.\"\n" +
		"    _unknown: \"Activite de maintenance enregistree.\"\n"
	if err := os.WriteFile(filepath.Join(dir, "en.yml"), []byte(en), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "fr.yml"), []byte(fr), 0o644); err != nil {
		t.Fatal(err)
	}
	tr, err := i18n.Load(dir)
	if err != nil {
		t.Fatal(err)
	}
	return tr
}

func writeLog(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "maintenance-log.json")
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestBuildEntriesLocalizedNewestFirst(t *testing.T) {
	tr := testTranslations(t)
	dir := writeLog(t, `[
	  {"ts":"2026-01-01T00:00:00Z","severity":"success","code":"backup_ok","params":{"time":"03:00","size":"1.2 GB"}},
	  {"ts":"2026-01-02T00:00:00Z","severity":"warn","code":"unmapped_code","params":{}}
	]`)
	got := BuildEntries(tr, "fr", dir, 50)
	if len(got) != 2 {
		t.Fatalf("len = %d, want 2", len(got))
	}
	// Newest-first: the unmapped (2026-01-02) row comes first, _unknown line.
	if got[0].Message != "Activite de maintenance enregistree." {
		t.Errorf("row0 message = %q, want the FR _unknown line", got[0].Message)
	}
	if got[0].Severity != "warn" {
		t.Errorf("row0 severity = %q, want warn", got[0].Severity)
	}
	// Known code interpolates params in FR.
	if got[1].Message != "Sauvegarde terminee a 03:00. Taille 1.2 GB." {
		t.Errorf("row1 message = %q", got[1].Message)
	}
}

func TestBuildEntriesNumericParamCoerced(t *testing.T) {
	tr := testTranslations(t)
	// count is a JSON number, not a string; must coerce without dropping the row.
	dir := writeLog(t, `[{"ts":"t","code":"backup_ok","params":{"time":"x","size":5}}]`)
	got := BuildEntries(tr, "en", dir, 50)
	if len(got) != 1 {
		t.Fatalf("len = %d, want 1", len(got))
	}
	if got[0].Message != "Backup completed at x. Size 5." {
		t.Errorf("message = %q, want numeric size coerced to 5", got[0].Message)
	}
}

func TestBuildEntriesMissingOrGarbage(t *testing.T) {
	tr := testTranslations(t)
	if got := BuildEntries(tr, "en", "/nonexistent", 50); len(got) != 0 {
		t.Errorf("missing log = %v, want empty", got)
	}
	dir := writeLog(t, `not json`)
	if got := BuildEntries(tr, "en", dir, 50); len(got) != 0 {
		t.Errorf("garbage log = %v, want empty", got)
	}
}

func TestBuildEntriesLimit(t *testing.T) {
	tr := testTranslations(t)
	dir := writeLog(t, `[
	  {"ts":"1","code":"backup_ok","params":{"time":"a","size":"b"}},
	  {"ts":"2","code":"backup_ok","params":{"time":"a","size":"b"}},
	  {"ts":"3","code":"backup_ok","params":{"time":"a","size":"b"}}
	]`)
	got := BuildEntries(tr, "en", dir, 2)
	if len(got) != 2 {
		t.Fatalf("len = %d, want 2 (limited)", len(got))
	}
	// Last two rows kept, newest-first: ts 3 then ts 2.
	if got[0].TS != "3" || got[1].TS != "2" {
		t.Errorf("limited window = %q,%q, want 3,2", got[0].TS, got[1].TS)
	}
}
