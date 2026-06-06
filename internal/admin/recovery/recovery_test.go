package recovery

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestListArtifactsSortsAndClassifies(t *testing.T) {
	dir := t.TempDir()
	// Three valid artifacts + one ignored extension.
	write := func(name string, age time.Duration) {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
		mt := time.Now().Add(-age)
		if err := os.Chtimes(p, mt, mt); err != nil {
			t.Fatal(err)
		}
	}
	write("recovery-20260101.zip", 3*time.Hour)
	write("snapshot-20260102.tar.gz", 1*time.Hour)
	write("misc.txt", 2*time.Hour)
	write("blob.zip", 2*time.Hour)

	got := ListArtifacts(dir, "https://recovery.example.com")
	if len(got) != 3 {
		t.Fatalf("len = %d, want 3 (misc.txt ignored)", len(got))
	}
	// Newest first: snapshot (1h) before recovery (3h); blob (2h) between.
	if got[0].Name != "snapshot-20260102.tar.gz" {
		t.Errorf("newest = %q, want snapshot", got[0].Name)
	}
	if got[0].Kind != "snapshot" {
		t.Errorf("kind = %q, want snapshot", got[0].Kind)
	}
	if got[2].Kind != "archive" {
		t.Errorf("oldest kind = %q, want archive", got[2].Kind)
	}
	for _, a := range got {
		if a.Name == "blob.zip" && a.Kind != "other" {
			t.Errorf("blob.zip kind = %q, want other", a.Kind)
		}
	}
	if got[0].DownloadURL != "https://recovery.example.com/snapshot-20260102.tar.gz" {
		t.Errorf("download URL = %q", got[0].DownloadURL)
	}
}

func TestListArtifactsNoSidecarOrMissingDir(t *testing.T) {
	if got := ListArtifacts("/nonexistent/path", ""); got != nil {
		t.Errorf("missing dir = %v, want nil", got)
	}
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "recovery-x.zip"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	got := ListArtifacts(dir, "")
	if len(got) != 1 || got[0].DownloadURL != "" {
		t.Errorf("no sidecar should yield empty DownloadURL, got %+v", got)
	}
}

func TestHumanSize(t *testing.T) {
	cases := map[int64]string{
		0:          "0 B",
		512:        "512 B",
		1024:       "1.0 KB",
		1536:       "1.5 KB",
		1048576:    "1.0 MB",
		1073741824: "1.0 GB",
	}
	for in, want := range cases {
		if got := HumanSize(in); got != want {
			t.Errorf("HumanSize(%d) = %q, want %q", in, got, want)
		}
	}
}

func TestIconEntity(t *testing.T) {
	if (Artifact{Kind: "archive"}).IconEntity() != "&#128190;" {
		t.Error("archive icon wrong")
	}
	if (Artifact{Kind: "other"}).IconEntity() != "&#128196;" {
		t.Error("other icon wrong")
	}
}
