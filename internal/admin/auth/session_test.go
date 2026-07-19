package auth

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

var testNow = time.Unix(1_700_000_000, 0)

func TestSessionRoundTrip(t *testing.T) {
	c := NewSessionCodec("a-strong-session-key")
	if c == nil {
		t.Fatal("NewSessionCodec returned nil for a non-blank key")
	}
	value, err := c.Encode(Session{Email: "op@example.com", Admin: true}, testNow)
	if err != nil {
		t.Fatalf("Encode: %v", err)
	}
	got, ok := c.Decode(value, testNow.Add(time.Hour))
	if !ok {
		t.Fatal("Decode: valid token rejected")
	}
	if got.Email != "op@example.com" || !got.Admin {
		t.Errorf("Decode = %+v, want op@example.com admin", got)
	}
}

func TestNewSessionCodecBlankKeyIsNil(t *testing.T) {
	if NewSessionCodec("") != nil || NewSessionCodec("   ") != nil {
		t.Error("blank key must return a nil codec (native login disabled)")
	}
}

func TestSessionTamperRejected(t *testing.T) {
	c := NewSessionCodec("key")
	value, _ := c.Encode(Session{Email: "op@example.com", Admin: true}, testNow)
	// Flip a byte in the payload segment.
	tampered := "x" + value[1:]
	if _, ok := c.Decode(tampered, testNow); ok {
		t.Error("tampered token accepted")
	}
}

func TestSessionExpiryRejected(t *testing.T) {
	c := NewSessionCodec("key")
	value, _ := c.Encode(Session{Email: "op@example.com", Admin: true}, testNow)
	if _, ok := c.Decode(value, testNow.Add(SessionTTL+time.Second)); ok {
		t.Error("expired token accepted")
	}
}

func TestSessionWrongKeyRejected(t *testing.T) {
	value, _ := NewSessionCodec("key-a").Encode(Session{Email: "op@example.com", Admin: true}, testNow)
	if _, ok := NewSessionCodec("key-b").Decode(value, testNow); ok {
		t.Error("token signed with a different key accepted")
	}
}

func TestIdentityFromSession(t *testing.T) {
	c := NewSessionCodec("key")
	value, _ := c.Encode(Session{Email: "op@example.com", Admin: true}, testNow)
	r := httptest.NewRequest("GET", "/settings", nil)
	r.AddCookie(&http.Cookie{Name: SessionCookieName, Value: value})
	id, ok := IdentityFromSession(c, r, testNow)
	if !ok {
		t.Fatal("IdentityFromSession: valid cookie rejected")
	}
	if !id.IsAdmin() || id.Email != "op@example.com" {
		t.Errorf("identity = %+v, want admin op@example.com", id)
	}

	// A non-admin session yields no admin group.
	value2, _ := c.Encode(Session{Email: "staff@example.com", Admin: false}, testNow)
	r2 := httptest.NewRequest("GET", "/", nil)
	r2.AddCookie(&http.Cookie{Name: SessionCookieName, Value: value2})
	id2, ok := IdentityFromSession(c, r2, testNow)
	if !ok || id2.IsAdmin() {
		t.Errorf("non-admin session = %+v (ok=%v), want authenticated non-admin", id2, ok)
	}
}

func TestIdentityFromSessionMissingCookie(t *testing.T) {
	c := NewSessionCodec("key")
	r := httptest.NewRequest("GET", "/", nil)
	if _, ok := IdentityFromSession(c, r, testNow); ok {
		t.Error("missing cookie must not authenticate")
	}
	if _, ok := IdentityFromSession(nil, r, testNow); ok {
		t.Error("nil codec must not authenticate")
	}
}

func TestVerifyLocalPassword(t *testing.T) {
	if !VerifyLocalPassword("s3cret", "s3cret") {
		t.Error("matching password rejected")
	}
	if VerifyLocalPassword("s3cret", "wrong") {
		t.Error("wrong password accepted")
	}
	if VerifyLocalPassword("", "") {
		t.Error("empty stored credential must never match")
	}
}
