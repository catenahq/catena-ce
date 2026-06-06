package license

import (
	"crypto/ed25519"
	"crypto/rand"
	"testing"
	"time"
)

func mustKeys(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("keygen: %v", err)
	}
	return pub, priv
}

func businessClaims(validUntil time.Time) Claims {
	return Claims{
		Subject:    "acme",
		Edition:    Business,
		IssuedAt:   validUntil.Add(-30 * 24 * time.Hour),
		ValidUntil: validUntil,
	}
}

func TestVerifyRoundTripAndActive(t *testing.T) {
	pub, priv := mustKeys(t)
	now := time.Date(2026, 6, 5, 12, 0, 0, 0, time.UTC)
	tok, err := Sign(businessClaims(now.Add(24*time.Hour)), priv)
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	lic, err := Verify(tok, pub)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if lic.Claims.Subject != "acme" || lic.Claims.Edition != Business {
		t.Fatalf("unexpected claims: %+v", lic.Claims)
	}
	if !lic.Active(now, 0) {
		t.Fatal("license should be active before expiry")
	}
}

func TestExpiryAndGrace(t *testing.T) {
	pub, priv := mustKeys(t)
	exp := time.Date(2026, 6, 5, 0, 0, 0, 0, time.UTC)
	tok, _ := Sign(businessClaims(exp), priv)
	lic, err := Verify(tok, pub)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	grace := 72 * time.Hour
	// Just past expiry but inside grace: still active.
	if !lic.Active(exp.Add(time.Hour), grace) {
		t.Fatal("should be active within grace window")
	}
	// Past expiry + grace: lapsed.
	if lic.Active(exp.Add(grace+time.Hour), grace) {
		t.Fatal("should be lapsed past the grace window")
	}
	// No grace: lapsed immediately after expiry.
	if lic.Active(exp.Add(time.Second), 0) {
		t.Fatal("should be lapsed with no grace")
	}
}

func TestForgedAndTamperedRefused(t *testing.T) {
	pub, priv := mustKeys(t)
	otherPub, _ := mustKeys(t)
	now := time.Now()
	tok, _ := Sign(businessClaims(now.Add(time.Hour)), priv)

	// Wrong public key -> signature failure.
	if _, err := Verify(tok, otherPub); err != ErrSignature {
		t.Fatalf("wrong key: want ErrSignature, got %v", err)
	}
	// Tampered payload (flip a char in the payload segment) -> signature
	// failure, never a silently-accepted claim change.
	tampered := []byte(tok)
	tampered[0] ^= 0x01
	if _, err := Verify(string(tampered), pub); err == nil {
		t.Fatal("tampered token must not verify")
	}
	// Malformed shapes.
	for _, bad := range []string{"", "onlyonepart", "a.b.c", ".", "x."} {
		if _, err := Verify(bad, pub); err == nil {
			t.Fatalf("malformed token %q must not verify", bad)
		}
	}
}

func TestCommunityTokenNeverActive(t *testing.T) {
	pub, priv := mustKeys(t)
	now := time.Now()
	c := businessClaims(now.Add(24 * time.Hour))
	c.Edition = Community
	tok, _ := Sign(c, priv)
	lic, err := Verify(tok, pub)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if lic.Active(now, 72*time.Hour) {
		t.Fatal("a community-edition token must never enable EE plugins")
	}
}

func TestNilLicenseInactive(t *testing.T) {
	var lic *License
	if lic.Active(time.Now(), time.Hour) {
		t.Fatal("nil license must be inactive (CE-only)")
	}
}
