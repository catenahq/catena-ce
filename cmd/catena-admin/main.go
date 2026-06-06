// Command catena-admin is the Catena Community shell: an HTTP admin
// surface that always hosts Community plugins and, when a Business license
// validates, the license-gated EE plugins pulled from catena-enterprise.
//
// This is the foundation skeleton. The CE actions/panels and the
// inline-SSH dispatch port over from the Python implementation in
// catenahq/ops (services/catena-admin) as the rewrite proceeds.
package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/catenahq/catena/internal/license"
	"github.com/catenahq/catena/internal/plugin"
)

var errInvalidPubkey = errors.New("license: CATENA_LICENSE_PUBKEY is not a valid base64 ed25519 public key")

// graceWindow is how long EE plugins keep working past ValidUntil when the
// license endpoint is unreachable. Generous on purpose: endpoint downtime
// must never brick a paying client.
const graceWindow = 72 * time.Hour

func main() {
	addr := envOr("CATENA_ADMIN_ADDR", ":8080")

	reg := plugin.NewRegistry()
	// CE plugins self-register here as they are ported. EE plugins, when
	// present, register from the license-gated binaries loaded at runtime.

	lic, licErr := loadLicense()
	if licErr != nil {
		log.Printf("license: %v (running Community-only)", licErr)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	mux.HandleFunc("/licensez", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(licenseStatus(lic, time.Now().UTC()))
	})

	enabled := reg.Enabled(lic, time.Now().UTC(), graceWindow)
	log.Printf("catena-admin on %s: edition=%s, %d plugin(s) enabled",
		addr, editionLabel(lic, time.Now().UTC()), len(enabled))
	log.Fatal(http.ListenAndServe(addr, mux))
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

func licenseStatus(lic *license.License, now time.Time) status {
	if lic == nil {
		return status{Edition: string(license.Community), Active: false}
	}
	return status{
		Edition:     string(lic.Claims.Edition),
		Active:      lic.Active(now, graceWindow),
		ValidUntil:  lic.Claims.ValidUntil.UTC().Format(time.RFC3339),
		LastUpdated: lic.Checked.Format(time.RFC3339),
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
