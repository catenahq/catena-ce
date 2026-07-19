// Native-login session for the direct (tailnet) listener. When Cloudflare +
// oauth2-proxy front the shell (dash.<zone>), identity arrives as trusted proxy
// headers (see auth.go). When the shell is reached directly on its host-
// published tailnet port there is no proxy, so this file gives it a local
// login: the operator authenticates against the on-box admin credential and
// gets a signed session cookie.
//
// The cookie is HMAC-SHA256 signed (integrity), not encrypted -- it carries
// only the email + admin flag + expiry, none of which is secret. Signing with
// a stable per-host key (CATENA_ADMIN_SESSION_KEY, minted on-box and carried in
// the backup) means a tailnet peer cannot forge a session without the key.
// stdlib only, no new dependency.
package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"strings"
	"time"
)

// SessionCookieName is the native-login session cookie.
const SessionCookieName = "catena_admin_session"

// SessionTTL is how long a native-login session stays valid.
const SessionTTL = 12 * time.Hour

// Session is the native-login session payload (direct listener only).
type Session struct {
	Email  string `json:"email"`
	Admin  bool   `json:"admin"`
	Expiry int64  `json:"exp"` // Unix seconds
}

// SessionCodec signs + verifies session cookies with a stable HMAC key.
type SessionCodec struct {
	key []byte
}

// NewSessionCodec builds a codec from the operator session key. A blank key
// returns nil: native login is not configured and the direct listener is not
// started (the caller checks for nil).
func NewSessionCodec(key string) *SessionCodec {
	if strings.TrimSpace(key) == "" {
		return nil
	}
	// Derive a fixed-width MAC key from the (variable-length base64) env value.
	sum := sha256.Sum256([]byte(key))
	return &SessionCodec{key: sum[:]}
}

func (c *SessionCodec) sign(payload []byte) string {
	mac := hmac.New(sha256.New, c.key)
	mac.Write(payload)
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// Encode returns a signed cookie value for s (Expiry is stamped from now).
func (c *SessionCodec) Encode(s Session, now time.Time) (string, error) {
	s.Expiry = now.Add(SessionTTL).Unix()
	payload, err := json.Marshal(s)
	if err != nil {
		return "", err
	}
	b64 := base64.RawURLEncoding.EncodeToString(payload)
	return b64 + "." + c.sign(payload), nil
}

// Decode verifies the signature + expiry and returns the session. A tampered,
// malformed, or expired token returns ok=false.
func (c *SessionCodec) Decode(value string, now time.Time) (Session, bool) {
	b64, sig, found := strings.Cut(value, ".")
	if !found {
		return Session{}, false
	}
	payload, err := base64.RawURLEncoding.DecodeString(b64)
	if err != nil {
		return Session{}, false
	}
	expected := c.sign(payload)
	if subtle.ConstantTimeCompare([]byte(sig), []byte(expected)) != 1 {
		return Session{}, false
	}
	var s Session
	if err := json.Unmarshal(payload, &s); err != nil {
		return Session{}, false
	}
	if s.Email == "" || now.Unix() >= s.Expiry {
		return Session{}, false
	}
	return s, true
}

// IdentityFromSession resolves a native-login Identity from the request's
// session cookie. A nil codec, missing cookie, or invalid session returns
// ok=false.
func IdentityFromSession(c *SessionCodec, r *http.Request, now time.Time) (Identity, bool) {
	if c == nil {
		return Identity{}, false
	}
	cookie, err := r.Cookie(SessionCookieName)
	if err != nil {
		return Identity{}, false
	}
	s, ok := c.Decode(cookie.Value, now)
	if !ok {
		return Identity{}, false
	}
	var groups []string
	if s.Admin {
		groups = []string{AdminGroup}
	}
	return Identity{Email: s.Email, DisplayName: s.Email, Groups: groups}, true
}

// VerifyLocalPassword constant-time compares the submitted password against the
// on-box admin credential (vault_admin_password in the /etc/catena/config.json
// store -- plaintext under 0b). An empty stored value never matches.
func VerifyLocalPassword(stored, submitted string) bool {
	if stored == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(stored), []byte(submitted)) == 1
}
