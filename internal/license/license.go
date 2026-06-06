// Package license validates Catena enterprise license tokens.
//
// A token is an ed25519-signed claim set. catena-admin verifies it
// OFFLINE with the operator public key and honours a grace window, so a
// transient license-endpoint outage never disables a paying client's EE
// plugins. The license endpoint (in catena-enterprise) mints tokens with
// the matching private key; Sign here is the single definition of the
// wire format both sides share.
package license

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

// Edition identifies the tier a deployment or plugin runs at.
type Edition string

const (
	Community Edition = "community"
	Business  Edition = "business"
)

// Claims is the signed payload of a license token.
type Claims struct {
	Subject    string    `json:"sub"`         // client / account id
	Edition    Edition   `json:"edition"`     // Business for a paid token
	IssuedAt   time.Time `json:"iat"`         // mint time
	ValidUntil time.Time `json:"valid_until"` // subscription boundary
}

// License is a verified token plus the moment it was checked (the "Last
// updated at Y" the shell shows under the license field).
type License struct {
	Claims  Claims
	Checked time.Time
}

// Errors are distinct so callers can tell a forged token from a lapsed
// one: a forged/garbled token is refused outright, a lapsed token is a
// known account whose subscription ended.
var (
	ErrMalformed = errors.New("license: malformed token")
	ErrSignature = errors.New("license: signature verification failed")
)

// Token wire format: base64url(payload) "." base64url(signature), where
// payload = JSON(Claims) signed with the operator ed25519 private key.

// Verify parses token and checks its ed25519 signature against pub. It
// does NOT check expiry; call Active for that, so the two failure modes
// stay separable.
func Verify(token string, pub ed25519.PublicKey) (*License, error) {
	parts := strings.Split(strings.TrimSpace(token), ".")
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return nil, ErrMalformed
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return nil, fmt.Errorf("%w: payload: %v", ErrMalformed, err)
	}
	sig, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, fmt.Errorf("%w: signature: %v", ErrMalformed, err)
	}
	if len(pub) != ed25519.PublicKeySize || !ed25519.Verify(pub, payload, sig) {
		return nil, ErrSignature
	}
	var c Claims
	if err := json.Unmarshal(payload, &c); err != nil {
		return nil, fmt.Errorf("%w: claims: %v", ErrMalformed, err)
	}
	return &License{Claims: c, Checked: time.Now().UTC()}, nil
}

// Active reports whether the license still entitles Business (EE) plugins
// at now, allowing grace beyond ValidUntil so endpoint downtime within
// the window is non-fatal. A nil or non-Business license is never active.
func (l *License) Active(now time.Time, grace time.Duration) bool {
	if l == nil || l.Claims.Edition != Business {
		return false
	}
	return !now.After(l.Claims.ValidUntil.Add(grace))
}

// Sign builds a token for c with priv. Used by the license endpoint and
// by tests; kept beside Verify so the wire format has one home.
func Sign(c Claims, priv ed25519.PrivateKey) (string, error) {
	payload, err := json.Marshal(c)
	if err != nil {
		return "", err
	}
	sig := ed25519.Sign(priv, payload)
	return base64.RawURLEncoding.EncodeToString(payload) + "." +
		base64.RawURLEncoding.EncodeToString(sig), nil
}
