// Package recovery assembles the Recovery tab's downloadable-artifact list,
// ported from the Python catena_admin.recovery. It walks the read-only
// bind-mount of the export directory; the bytes themselves are served by the
// oauth2-proxy-gated recovery nginx sidecar, not by the shell.
package recovery

import (
	"fmt"
	"os"
	"sort"
	"strings"
	"time"
)

const (
	exportsDirEnv      = "CATENA_ADMIN_EXPORTS_DIR"
	exportsDirFallback = "/var/backups/catena-export"
)

// supportedSuffixes are the only extensions surfaced; lock files / partial
// uploads are ignored so the operator only sees finished artifacts.
var supportedSuffixes = []string{".zip", ".tar.gz"}

// Artifact is one downloadable file in the export directory. DownloadURL is
// rendered at assemble-time (empty when no recovery sidecar URL is configured;
// the template then omits the link).
type Artifact struct {
	Name        string
	SizeBytes   int64
	ModTime     time.Time
	Kind        string // "archive" | "snapshot" | "other"
	DownloadURL string
}

func defaultExportsDir() string {
	if v := strings.TrimSpace(os.Getenv(exportsDirEnv)); v != "" {
		return v
	}
	return exportsDirFallback
}

// ListArtifacts walks exportsDir (empty = env override / default) and returns
// the artifacts newest-first. A missing or unreadable directory yields nil so
// a host without the sidecar renders the empty state rather than erroring.
func ListArtifacts(exportsDir, recoveryURLBase string) []Artifact {
	dir := exportsDir
	if dir == "" {
		dir = defaultExportsDir()
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var out []Artifact
	for _, e := range entries {
		if e.IsDir() || !hasSupportedSuffix(e.Name()) {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		out = append(out, Artifact{
			Name:        e.Name(),
			SizeBytes:   info.Size(),
			ModTime:     info.ModTime(),
			Kind:        kindOf(e.Name()),
			DownloadURL: downloadURL(recoveryURLBase, e.Name()),
		})
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].ModTime.After(out[j].ModTime) })
	return out
}

func hasSupportedSuffix(name string) bool {
	lower := strings.ToLower(name)
	for _, s := range supportedSuffixes {
		if strings.HasSuffix(lower, s) {
			return true
		}
	}
	return false
}

// kindOf distinguishes the encrypted DR archive (recovery-*.zip) from a restic
// snapshot export (snapshot-*.tar.gz) so the template can pick an icon.
func kindOf(name string) string {
	lower := strings.ToLower(name)
	switch {
	case strings.HasPrefix(lower, "recovery-") && strings.HasSuffix(lower, ".zip"):
		return "archive"
	case strings.HasPrefix(lower, "snapshot-") && strings.HasSuffix(lower, ".tar.gz"):
		return "snapshot"
	default:
		return "other"
	}
}

func downloadURL(base, filename string) string {
	if base == "" {
		return ""
	}
	return strings.TrimRight(base, "/") + "/" + filename
}

// HumanSizeOf renders the artifact's size; method form so templates call
// {{ .HumanSize }} without a FuncMap helper.
func (a Artifact) HumanSize() string { return HumanSize(a.SizeBytes) }

// FormatModTime renders the mtime as "YYYY-MM-DD HH:MM UTC" (locale-neutral;
// EN and FR audiences see the same UTC timestamp).
func (a Artifact) FormatModTime() string {
	return a.ModTime.UTC().Format("2006-01-02 15:04 UTC")
}

// IconEntity is the static numeric HTML entity for the artifact kind. Constant,
// not user input, so the template renders it through safeHTML.
func (a Artifact) IconEntity() string {
	switch a.Kind {
	case "archive":
		return "&#128190;"
	case "snapshot":
		return "&#128229;"
	default:
		return "&#128196;"
	}
}

// HumanSize renders n bytes as a short human-readable size. Locale-neutral --
// the SI-initial unit names are not translated.
func HumanSize(n int64) string {
	if n < 0 {
		n = 0
	}
	f := float64(n)
	for _, unit := range []string{"B", "KB", "MB", "GB", "TB"} {
		if f < 1024 {
			if unit == "B" {
				return fmt.Sprintf("%d %s", int64(f), unit)
			}
			return fmt.Sprintf("%.1f %s", f, unit)
		}
		f /= 1024
	}
	return fmt.Sprintf("%.1f PB", f)
}
