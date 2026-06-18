// Package pull is the shell-side client of the license endpoint. With an
// active Business license, the catena-admin shell calls Sync to fetch the EE
// plugin binaries for its architecture into the plugins dir; the loader then
// launches them. sha256 is the version identity: an unchanged binary is
// skipped, so Sync is cheap to run hourly and idempotent.
//
// The shell carries no bucket secret -- it presents only the client's license
// token; the endpoint holds the artifact store and decides what to hand back.
// Invalid/lapsed token -> the endpoint returns nothing and Sync is a no-op.
package pull

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"time"
)

// Artifact mirrors the endpoint's listing entry.
type Artifact struct {
	Name   string `json:"name"`
	OS     string `json:"os"`
	Arch   string `json:"arch"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

// Client pulls EE plugin binaries from the license endpoint.
type Client struct {
	Endpoint   string // base URL, e.g. https://license.catena.run
	Token      string // the client's signed license token
	PluginsDir string
	OS, Arch   string
	HTTP       *http.Client
}

// New builds a Client with sensible defaults (runtime os/arch, a 30s HTTP
// timeout). Endpoint and token come from the shell env.
func New(endpoint, token, pluginsDir string) *Client {
	return &Client{
		Endpoint:   endpoint,
		Token:      token,
		PluginsDir: pluginsDir,
		OS:         runtime.GOOS,
		Arch:       runtime.GOARCH,
		HTTP:       &http.Client{Timeout: 30 * time.Second},
	}
}

func (c *Client) archQuery() string {
	q := url.Values{}
	q.Set("os", c.OS)
	q.Set("arch", c.Arch)
	return q.Encode()
}

// List returns the artifacts the endpoint offers for this client + arch.
func (c *Client) List(ctx context.Context) ([]Artifact, error) {
	u := fmt.Sprintf("%s/v1/plugins?%s", c.Endpoint, c.archQuery())
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.Token)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("pull: list: endpoint returned %s", resp.Status)
	}
	var arts []Artifact
	if err := json.NewDecoder(resp.Body).Decode(&arts); err != nil {
		return nil, fmt.Errorf("pull: list: decode: %w", err)
	}
	// Every artifact name is joined onto PluginsDir to form a download target
	// and a hash path. Reject anything that is not a plain base filename so a
	// buggy or hostile endpoint cannot traverse out of PluginsDir.
	for _, a := range arts {
		if a.Name == "" || a.Name != filepath.Base(a.Name) {
			return nil, fmt.Errorf("pull: list: unsafe artifact name %q", a.Name)
		}
	}
	return arts, nil
}

// Sync lists then downloads every artifact whose local copy is missing or
// hashes differently. Returns the number of binaries downloaded this run.
func (c *Client) Sync(ctx context.Context) (int, error) {
	arts, err := c.List(ctx)
	if err != nil {
		return 0, err
	}
	if err := os.MkdirAll(c.PluginsDir, 0o750); err != nil {
		return 0, fmt.Errorf("pull: mkdir %s: %w", c.PluginsDir, err)
	}
	downloaded := 0
	for _, a := range arts {
		target := filepath.Join(c.PluginsDir, a.Name)
		if cur, err := localSHA256(target); err == nil && cur == a.SHA256 {
			continue // already current
		}
		if err := c.download(ctx, a); err != nil {
			return downloaded, err
		}
		downloaded++
	}
	return downloaded, nil
}

// download streams one artifact to a temp file in PluginsDir, verifies its
// sha256, then atomically renames it into place with the executable bit set.
// A hash mismatch removes the temp file and errors -- a corrupted or swapped
// binary never lands where the loader would launch it.
func (c *Client) download(ctx context.Context, a Artifact) error {
	u := fmt.Sprintf("%s/v1/plugins/%s?%s", c.Endpoint, url.PathEscape(a.Name), c.archQuery())
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.Token)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("pull: download %s: endpoint returned %s", a.Name, resp.Status)
	}

	tmp, err := os.CreateTemp(c.PluginsDir, "."+a.Name+".*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op after a successful rename

	h := sha256.New()
	if _, err := io.Copy(io.MultiWriter(tmp, h), resp.Body); err != nil {
		_ = tmp.Close() // already returning the copy error; temp file is removed by defer
		return fmt.Errorf("pull: write %s: %w", a.Name, err)
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if got := hex.EncodeToString(h.Sum(nil)); got != a.SHA256 {
		return fmt.Errorf("pull: %s sha256 mismatch: want %s got %s", a.Name, a.SHA256, got)
	}
	// 0o700: the loader execs this binary as the admin user, so the owner exec
	// bit is required; group/other get nothing. (gosec G302 wants <=0600, but a
	// plugin binary must be executable -- 0o700 is the minimal workable mode.)
	if err := os.Chmod(tmpName, 0o700); err != nil { // #nosec G302 -- plugin binary must be owner-executable
		return err
	}
	return os.Rename(tmpName, filepath.Join(c.PluginsDir, a.Name))
}

func localSHA256(path string) (string, error) {
	// path is PluginsDir joined with an artifact name already validated in
	// List() to be a plain base filename, so no traversal is possible here.
	f, err := os.Open(path) // #nosec G304 -- artifact name validated to a base filename in List()
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
