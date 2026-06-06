package main

import (
	"testing"
	"time"

	"github.com/catenahq/catena-ce/license"
)

func businessLicense(validUntil time.Time) *license.License {
	return &license.License{
		Claims: license.Claims{
			Subject: "acme", Edition: license.Business,
			IssuedAt: time.Now().Add(-time.Hour), ValidUntil: validUntil,
		},
		Checked: time.Date(2026, 6, 6, 9, 0, 0, 0, time.UTC),
	}
}

func TestLicenseStatusNilIsCommunity(t *testing.T) {
	s := licenseStatus(nil, time.Now(), time.Time{})
	if s.Edition != string(license.Community) || s.Active {
		t.Fatalf("nil license should be inactive community, got %+v", s)
	}
}

func TestLicenseStatusActiveUsesLastPull(t *testing.T) {
	now := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	lastPull := time.Date(2026, 6, 6, 11, 30, 0, 0, time.UTC)
	s := licenseStatus(businessLicense(now.Add(30*24*time.Hour)), now, lastPull)
	if !s.Active {
		t.Fatal("license within validity should be active")
	}
	if s.LastUpdated != lastPull.Format(time.RFC3339) {
		t.Fatalf("LastUpdated should be the last pull, got %s", s.LastUpdated)
	}
}

func TestLicenseStatusFallsBackToCheckedBeforeFirstPull(t *testing.T) {
	now := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	lic := businessLicense(now.Add(time.Hour))
	s := licenseStatus(lic, now, time.Time{})
	if s.LastUpdated != lic.Checked.Format(time.RFC3339) {
		t.Fatalf("with no pull yet, LastUpdated should fall back to Checked, got %s", s.LastUpdated)
	}
}

func TestLicenseStatusLapsedIsInactive(t *testing.T) {
	now := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	// Past validity AND past the grace window.
	lapsed := businessLicense(now.Add(-graceWindow - time.Hour))
	s := licenseStatus(lapsed, now, time.Time{})
	if s.Active {
		t.Fatal("a license past grace must be inactive")
	}
}

func TestPullStateRoundTrip(t *testing.T) {
	st := &pullState{}
	if !st.get().IsZero() {
		t.Fatal("fresh pullState should be zero")
	}
	now := time.Now().UTC()
	st.set(now)
	if !st.get().Equal(now) {
		t.Fatalf("want %v, got %v", now, st.get())
	}
}
