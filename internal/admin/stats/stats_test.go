package stats

import (
	"os"
	"path/filepath"
	"testing"
)

func write(t *testing.T, dir, name, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestDirPrecedence(t *testing.T) {
	t.Setenv("CATENA_ADMIN_STATS_DIR", "/from-env")
	if got := Dir("/override"); got != "/override" {
		t.Fatalf("override should win, got %q", got)
	}
	if got := Dir("  "); got != "/from-env" {
		t.Fatalf("env should win over default, got %q", got)
	}
	t.Setenv("CATENA_ADMIN_STATS_DIR", "")
	if got := Dir(""); got != "/var/lib/catena" {
		t.Fatalf("default expected, got %q", got)
	}
}

func TestReadHappyPath(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "backup-stats.json", `{"snapshot_count": 7, "repo_size_human": "1.2 GiB"}`)
	m := Backup(dir)
	if Int(m, "snapshot_count") != 7 {
		t.Fatalf("snapshot_count: got %v", m["snapshot_count"])
	}
	if String(m, "repo_size_human") != "1.2 GiB" {
		t.Fatalf("repo_size_human: got %v", m["repo_size_human"])
	}
}

func TestReadFallsBackToEmptyMap(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "bad.json", "{not json")
	write(t, dir, "null.json", "null")
	for _, name := range []string{"missing", "bad", "null"} {
		m := Read(name, dir)
		if m == nil || len(m) != 0 {
			t.Fatalf("%s: expected empty map, got %v", name, m)
		}
	}
}

func TestReadGuardsPathTraversal(t *testing.T) {
	dir := t.TempDir()
	sub := filepath.Join(dir, "sub")
	if err := os.Mkdir(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	write(t, dir, "secret.json", `{"leak": "yes"}`)
	for _, name := range []string{"", "../secret", "sub/../secret", "/etc/passwd"} {
		if m := Read(name, sub); len(m) != 0 {
			t.Fatalf("traversal name %q recovered data: %v", name, m)
		}
	}
}

func TestNamedReadersUseTheirFiles(t *testing.T) {
	dir := t.TempDir()
	cases := map[string]func(string) map[string]any{
		"verify-hot":             VerifyHot,
		"verify-cold":            VerifyCold,
		"restic-check":           ResticCheck,
		"container-cve-findings": ContainerCVE,
		"homepage-summary":       HomepageSummary,
		"port-scan":              PortScan,
	}
	for file, fn := range cases {
		write(t, dir, file+".json", `{"marker": "`+file+`"}`)
		if got := String(fn(dir), "marker"); got != file {
			t.Fatalf("%s reader: got marker %q", file, got)
		}
	}
}

func TestIntCoercesJSONNumbers(t *testing.T) {
	m := map[string]any{"f": float64(3), "i": 4, "s": "x"}
	if Int(m, "f") != 3 || Int(m, "i") != 4 {
		t.Fatal("numeric coercion failed")
	}
	if Int(m, "s") != 0 || Int(m, "absent") != 0 {
		t.Fatal("non-numeric should yield 0")
	}
}

func TestHasDistinguishesAbsentFromZero(t *testing.T) {
	m := map[string]any{"zero": float64(0), "nil": nil}
	if !Has(m, "zero") {
		t.Fatal("present-and-zero should be Has=true")
	}
	if Has(m, "nil") || Has(m, "absent") {
		t.Fatal("nil / absent should be Has=false")
	}
}
