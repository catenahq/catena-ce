// Package auth is the catena-admin shell's identity + admin authorization,
// ported from the Python catena_admin.auth. oauth2-proxy in front of the
// shell injects the signed-in identity as request headers:
//
//	X-Forwarded-Email               the user's email
//	X-Forwarded-Preferred-Username  display name
//	X-Forwarded-Groups              comma- or newline-separated Keycloak groups
//
// The proxy is the gate; this package trusts whoever it names. Defense in
// depth: when CATENA_ADMIN_REQUIRE_HEADER_SIG is opted in and a secret is
// set, the shell verifies an HMAC-SHA256 over email|groups|preferred_username
// presented as X-Catena-Sig, so a co-tenant on the cluster network cannot
// spoof the identity headers without the shared secret.
package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"os"
	"sort"
	"strings"
)

// Four-tier group model (see roles/keycloak realm-vps). admin is the operator
// superuser; staff the SMB employees; client the SMB external users; visitor
// a label meaning "public" (never carried in a real token).
const (
	AdminGroup  = "admin"
	StaffGroup  = "staff"
	ClientGroup = "client"
	Visitor     = "visitor"
)

const (
	emailHeader     = "X-Forwarded-Email"
	usernameHeader  = "X-Forwarded-Preferred-Username"
	groupsHeader    = "X-Forwarded-Groups"
	sigHeader       = "X-Catena-Sig"
	requireSigEnv   = "CATENA_ADMIN_REQUIRE_HEADER_SIG"
	headerSecretEnv = "CATENA_ADMIN_HEADER_SECRET"
)

// Sig verification errors. The HTTP layer maps both to 403.
var (
	ErrMissingSig  = errors.New("auth: missing X-Catena-Sig")
	ErrSigMismatch = errors.New("auth: X-Catena-Sig mismatch")
)

// Identity is the immutable signed-in user resolved from the proxy headers.
type Identity struct {
	Email       string
	DisplayName string
	Groups      []string // deduped, sorted
}

// IsAuthenticated reports whether the proxy named a user.
func (i Identity) IsAuthenticated() bool { return i.Email != "" }

// IsAdmin reports membership in the operator admin group.
func (i Identity) IsAdmin() bool { return i.HasGroup(AdminGroup) }

// HasGroup reports membership in g.
func (i Identity) HasGroup(g string) bool {
	for _, x := range i.Groups {
		if x == g {
			return true
		}
	}
	return false
}

// ParseGroups splits the X-Forwarded-Groups value, which oauth2-proxy emits
// comma-separated by default and some Keycloak mappers emit newline-separated.
// It accepts both, trims, dedups, and sorts for stable output.
func ParseGroups(raw string) []string {
	if raw == "" {
		return nil
	}
	seen := make(map[string]struct{})
	var out []string
	for _, chunk := range strings.Split(strings.ReplaceAll(raw, "\n", ","), ",") {
		chunk = strings.TrimSpace(chunk)
		if chunk == "" {
			continue
		}
		if _, dup := seen[chunk]; dup {
			continue
		}
		seen[chunk] = struct{}{}
		out = append(out, chunk)
	}
	sort.Strings(out)
	return out
}

// IdentityFromRequest verifies the header signature (when required) and parses
// the proxy headers into an Identity. A sig failure returns a non-nil error
// the HTTP layer maps to 403; the Identity is zero in that case.
func IdentityFromRequest(r *http.Request) (Identity, error) {
	if err := verifyHeaderSig(r); err != nil {
		return Identity{}, err
	}
	return Identity{
		Email:       strings.TrimSpace(r.Header.Get(emailHeader)),
		DisplayName: strings.TrimSpace(r.Header.Get(usernameHeader)),
		Groups:      ParseGroups(r.Header.Get(groupsHeader)),
	}, nil
}

// headerSigRequired is true only when the operator opted in AND a secret is
// set, so flipping the flag without a secret fails closed (callers surface a
// config error) rather than silently always-allowing.
func headerSigRequired() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(requireSigEnv))) {
	case "1", "true", "yes":
		return strings.TrimSpace(os.Getenv(headerSecretEnv)) != ""
	}
	return false
}

// computeHeaderSig is HMAC-SHA256 over email|groups|preferred_username with
// the operator secret. The "|" join (not JSON) keeps the byte sequence stable
// across Python and Go encoders.
func computeHeaderSig(email, groups, preferredUsername string) string {
	mac := hmac.New(sha256.New, []byte(os.Getenv(headerSecretEnv)))
	mac.Write([]byte(email + "|" + groups + "|" + preferredUsername))
	return hex.EncodeToString(mac.Sum(nil))
}

// verifyHeaderSig is a no-op unless required; otherwise it rejects a request
// whose X-Catena-Sig is absent or does not match the recomputed digest.
func verifyHeaderSig(r *http.Request) error {
	if !headerSigRequired() {
		return nil
	}
	presented := strings.TrimSpace(r.Header.Get(sigHeader))
	if presented == "" {
		return ErrMissingSig
	}
	expected := computeHeaderSig(
		strings.TrimSpace(r.Header.Get(emailHeader)),
		r.Header.Get(groupsHeader),
		strings.TrimSpace(r.Header.Get(usernameHeader)),
	)
	// Constant-time compare so a remote attacker cannot recover the digest
	// one byte at a time via response timing.
	if !hmac.Equal([]byte(presented), []byte(expected)) {
		return ErrSigMismatch
	}
	return nil
}
