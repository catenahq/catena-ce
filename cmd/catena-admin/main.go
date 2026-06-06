// Command catena-admin is the Catena Community shell: a server-rendered web
// admin panel that always hosts Community pages and, when a Business license
// validates, the license-gated EE plugins pulled from catena-ee.
//
// The CE tab routes (Apps/System/Actions/Recovery) port over from the Python
// implementation in catenahq/ops (services/catena-admin) as the rewrite
// proceeds; this wires the shell server, the license check, and EE plugin
// loading together.
package main

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/catenahq/catena-ce/internal/admin/actions"
	"github.com/catenahq/catena-ce/internal/admin/integrations"
	"github.com/catenahq/catena-ce/internal/admin/web"
	"github.com/catenahq/catena-ce/internal/pull"
	"github.com/catenahq/catena-ce/internal/registry"
	"github.com/catenahq/catena-ce/license"
	"github.com/catenahq/catena-ce/loader"
)

// version is the shell build version surfaced at /health. Stamped at release.
const version = "0.0.0-dev"

var errInvalidPubkey = errors.New("license: CATENA_LICENSE_PUBKEY is not a valid base64 ed25519 public key")

// graceWindow is how long EE plugins keep working past ValidUntil when the
// license endpoint is unreachable. Generous on purpose: endpoint downtime
// must never brick a paying client.
const graceWindow = 72 * time.Hour

func main() {
	addr := envOr("CATENA_ADMIN_ADDR", ":8080")

	now := time.Now().UTC()

	reg := registry.New()
	// CE plugins self-register here as they are ported. EE plugins, when
	// present, register from the license-gated binaries loaded at runtime.

	lic, licErr := loadLicense()
	if licErr != nil {
		log.Printf("license: %v (running Community-only)", licErr)
	}

	// pulls tracks the last successful plugin pull -- the "Last updated at Y"
	// the shell shows under the license field.
	pulls := &pullState{}
	pluginsDir := envOr("CATENA_PLUGINS_DIR", "/var/lib/catena/plugins")
	endpoint := strings.TrimSpace(os.Getenv("CATENA_LICENSE_ENDPOINT"))

	// EE plugin binaries are only fetched + spawned behind an active license;
	// CE-only hosts never reach the endpoint or launch a binary. The registry's
	// edition gate is the backstop.
	if lic.Active(now, graceWindow) {
		if endpoint != "" {
			puller := pull.New(endpoint, strings.TrimSpace(os.Getenv("CATENA_LICENSE")), pluginsDir)
			if n, err := puller.Sync(context.Background()); err != nil {
				log.Printf("plugins: initial pull from %s: %v", endpoint, err)
			} else {
				pulls.set(time.Now().UTC())
				log.Printf("plugins: pulled %d binary(ies) from %s", n, endpoint)
			}
			// Re-validate + re-pull hourly; stops when the license lapses.
			go pullLoop(context.Background(), puller, pulls, lic)
		}
		if n := loadEEPlugins(reg, pluginsDir); n > 0 {
			log.Printf("plugins: loaded %d EE plugin(s) from %s", n, pluginsDir)
		}
	}

	// Read-only status clients the System (and later Apps) tab consume.
	// Internal cluster URLs, same defaults as the Python shell.
	gatusBase := fmt.Sprintf("http://%s:%s",
		envOr("GATUS_COMPOSE_NAME", "gatus"), envOr("GATUS_INTERNAL_PORT", "80"))
	hcBase := fmt.Sprintf("http://%s:%s",
		envOr("HEALTHCHECKS_COMPOSE_NAME", "healthchecks"), envOr("HEALTHCHECKS_INTERNAL_PORT", "8000"))

	// The shell web app (CE pages, i18n, theme, auth) serves everything
	// except the license status probe below.
	shell, err := web.New(web.Config{
		Version:         version,
		Globals:         web.GlobalsFromEnv(),
		TranslationsDir: strings.TrimSpace(os.Getenv("CATENA_ADMIN_TRANSLATIONS_DIR")),
		Gatus:           integrations.NewGatusClient(gatusBase),
		Healthchecks:    integrations.NewHealthchecksClient(hcBase, os.Getenv("HEALTHCHECKS_API_KEY_READONLY")),
		Dokploy: integrations.NewDokployClient(
			envOr("DOKPLOY_API_BASE", "http://127.0.0.1:3000"), os.Getenv("DOKPLOY_API_KEY")),
		// Recovery sidecar base URL: artifact Download links point here; the
		// oauth2-proxy-gated nginx sidecar serves the bytes, not the shell.
		// ExportsDir is left empty so the recovery package uses its env/default.
		RecoveryURL: strings.TrimSpace(os.Getenv("CATENA_ADMIN_RECOVERY_URL")),
		// The single host-dispatch seam: every Actions/Recovery click runs
		// through this SSH runner to the forced-command host account. EE plugin
		// actions reuse it (they never hold the key themselves).
		Runner: actions.NewSSHRunner(),
	})
	if err != nil {
		log.Fatalf("catena-admin: build shell: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/licensez", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(licenseStatus(lic, time.Now().UTC(), pulls.get()))
	})
	mux.Handle("/", shell)

	enabled := reg.Enabled(lic, now, graceWindow)
	log.Printf("catena-admin on %s: edition=%s, %d plugin(s) enabled",
		addr, editionLabel(lic, now), len(enabled))
	log.Fatal(http.ListenAndServe(addr, mux))
}

// pullState tracks the last successful plugin pull, read by /licensez. Guarded
// because the hourly pull loop writes it from a background goroutine.
type pullState struct {
	mu   sync.Mutex
	last time.Time
}

func (s *pullState) set(t time.Time) {
	s.mu.Lock()
	s.last = t
	s.mu.Unlock()
}

func (s *pullState) get() time.Time {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.last
}

// pullLoop re-pulls plugin binaries hourly while the license is active, and
// exits once it lapses (past grace) so a cancelled subscription stops pulling.
// Newly pulled binaries take effect on the next shell restart; live hot-reload
// of the registry arrives with the plugin-render wiring (M2.5).
func pullLoop(ctx context.Context, puller *pull.Client, st *pullState, lic *license.License) {
	t := time.NewTicker(time.Hour)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if !lic.Active(time.Now().UTC(), graceWindow) {
				log.Printf("plugins: license lapsed; stopping hourly pull")
				return
			}
			n, err := puller.Sync(ctx)
			if err != nil {
				log.Printf("plugins: hourly pull: %v", err)
				continue
			}
			st.set(time.Now().UTC())
			if n > 0 {
				log.Printf("plugins: pulled %d updated binary(ies) (effective on restart)", n)
			}
		}
	}
}

// loadEEPlugins launches every executable in dir as a go-plugin binary and
// registers the ones that dispense cleanly. A missing dir is normal (no EE
// plugins pulled yet) and returns 0. The child processes outlive this call
// for the life of the server; the OS reaps them on exit.
func loadEEPlugins(reg *registry.Registry, dir string) int {
	entries, err := os.ReadDir(dir)
	if err != nil {
		if !os.IsNotExist(err) {
			log.Printf("plugins: read %s: %v", dir, err)
		}
		return 0
	}
	count := 0
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		info, err := e.Info()
		if err != nil || info.Mode()&0o111 == 0 {
			continue // skip non-executables
		}
		path := filepath.Join(dir, e.Name())
		p, _, err := loader.Load(path)
		if err != nil {
			log.Printf("plugins: %s: %v", e.Name(), err)
			continue
		}
		reg.Register(p)
		count++
	}
	return count
}

// loadLicense reads the token (CATENA_LICENSE) and operator public key
// (CATENA_LICENSE_PUBKEY, base64 raw ed25519) from the environment. With
// either absent the host runs Community-only (nil license, no error). A
// present-but-invalid token is an error the caller logs, still CE-only.
func loadLicense() (*license.License, error) {
	token := strings.TrimSpace(os.Getenv("CATENA_LICENSE"))
	pubB64 := strings.TrimSpace(os.Getenv("CATENA_LICENSE_PUBKEY"))
	if token == "" || pubB64 == "" {
		return nil, nil
	}
	raw, err := base64.StdEncoding.DecodeString(pubB64)
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return nil, errInvalidPubkey
	}
	return license.Verify(token, ed25519.PublicKey(raw))
}

// licenseStatus is the JSON behind /licensez and the source of the
// "Valid until X. Last updated at Y" label the shell shows under the
// license field.
type status struct {
	Edition     string `json:"edition"`
	Active      bool   `json:"active"`
	ValidUntil  string `json:"valid_until,omitempty"`
	LastUpdated string `json:"last_updated,omitempty"`
}

// licenseStatus builds the /licensez payload. lastPull is the moment the
// shell last fetched plugin binaries; it is the "Last updated at Y" the label
// shows. Before any pull (or CE-only) it falls back to the token check time.
func licenseStatus(lic *license.License, now, lastPull time.Time) status {
	if lic == nil {
		return status{Edition: string(license.Community), Active: false}
	}
	updated := lastPull
	if updated.IsZero() {
		updated = lic.Checked
	}
	return status{
		Edition:     string(lic.Claims.Edition),
		Active:      lic.Active(now, graceWindow),
		ValidUntil:  lic.Claims.ValidUntil.UTC().Format(time.RFC3339),
		LastUpdated: updated.UTC().Format(time.RFC3339),
	}
}

func editionLabel(lic *license.License, now time.Time) license.Edition {
	if lic.Active(now, graceWindow) {
		return license.Business
	}
	return license.Community
}

func envOr(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}
