package pull

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// fakeEndpoint serves one artifact, optionally with a corrupted sha so we can
// exercise the integrity check. It records whether a Bearer token arrived.
func fakeEndpoint(t *testing.T, payload []byte, badSHA bool, sawAuth *bool) *httptest.Server {
	t.Helper()
	sum := sha256.Sum256(payload)
	advertised := hex.EncodeToString(sum[:])
	if badSHA {
		advertised = strings.Repeat("0", 64)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/plugins", func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") {
			*sawAuth = true
		}
		_ = json.NewEncoder(w).Encode([]Artifact{{
			Name: "catena-audit", OS: r.URL.Query().Get("os"),
			Arch: r.URL.Query().Get("arch"), SHA256: advertised, Size: int64(len(payload)),
		}})
	})
	mux.HandleFunc("GET /v1/plugins/{name}", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(payload)
	})
	return httptest.NewServer(mux)
}

func newClient(t *testing.T, endpoint string) *Client {
	t.Helper()
	c := New(endpoint, "fake-token", t.TempDir())
	c.OS, c.Arch = runtime.GOOS, runtime.GOARCH
	return c
}

func TestSyncDownloadsAndIsExecutable(t *testing.T) {
	payload := []byte("plugin-binary-v1")
	var sawAuth bool
	srv := fakeEndpoint(t, payload, false, &sawAuth)
	defer srv.Close()

	c := newClient(t, srv.URL)
	n, err := c.Sync(context.Background())
	if err != nil {
		t.Fatalf("sync: %v", err)
	}
	if n != 1 {
		t.Fatalf("want 1 download, got %d", n)
	}
	if !sawAuth {
		t.Fatal("endpoint never saw the Bearer token")
	}
	target := filepath.Join(c.PluginsDir, "catena-audit")
	got, err := os.ReadFile(target)
	if err != nil || string(got) != string(payload) {
		t.Fatalf("bad downloaded content: %v / %q", err, got)
	}
	fi, _ := os.Stat(target)
	if fi.Mode()&0o111 == 0 {
		t.Fatalf("plugin binary not executable: %v", fi.Mode())
	}
}

func TestSyncIsIdempotent(t *testing.T) {
	var sawAuth bool
	srv := fakeEndpoint(t, []byte("plugin-binary-v1"), false, &sawAuth)
	defer srv.Close()

	c := newClient(t, srv.URL)
	if _, err := c.Sync(context.Background()); err != nil {
		t.Fatalf("first sync: %v", err)
	}
	n, err := c.Sync(context.Background())
	if err != nil {
		t.Fatalf("second sync: %v", err)
	}
	if n != 0 {
		t.Fatalf("second sync should download nothing, got %d", n)
	}
}

func TestSyncRejectsBadSHA(t *testing.T) {
	var sawAuth bool
	srv := fakeEndpoint(t, []byte("plugin-binary-v1"), true, &sawAuth)
	defer srv.Close()

	c := newClient(t, srv.URL)
	_, err := c.Sync(context.Background())
	if err == nil {
		t.Fatal("expected sha256 mismatch error")
	}
	if _, statErr := os.Stat(filepath.Join(c.PluginsDir, "catena-audit")); !os.IsNotExist(statErr) {
		t.Fatal("a corrupted binary must not land in the plugins dir")
	}
}

func TestListPropagatesNon200(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer srv.Close()
	c := newClient(t, srv.URL)
	if _, err := c.List(context.Background()); err == nil {
		t.Fatal("expected error on 403")
	}
}
