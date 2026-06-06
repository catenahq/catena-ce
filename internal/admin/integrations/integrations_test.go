package integrations

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestGatusListAndByHost(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/endpoints/statuses" {
			t.Errorf("unexpected path %q", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`[
			{"name":"nextcloud-internal","group":"apps","url":"https://cloud.example.com/","results":[{"success":false,"timestamp":"2026-06-05T10:00:00Z"}]},
			{"name":"nextcloud-public-302","group":"apps","url":"https://cloud.example.com/","results":[{"success":true,"timestamp":"2026-06-05T10:01:00Z"}]},
			{"name":"no-probe","group":"apps","url":"https://x.example.com/","results":[]}
		]`))
	}))
	defer srv.Close()

	c := NewGatusClient(srv.URL, WithGatusHTTPClient(srv.Client()))
	all := c.ListStatuses()
	if len(all) != 3 {
		t.Fatalf("ListStatuses len = %d, want 3", len(all))
	}

	// host preference: the public/302 entry wins over the internal one.
	s, ok := c.GetStatusByHost("cloud.example.com")
	if !ok {
		t.Fatal("expected a status for cloud.example.com")
	}
	if s.Healthy == nil || !*s.Healthy {
		t.Errorf("expected the public (healthy) entry to win, got %+v", s)
	}
	if s.LastCheckTS == nil {
		t.Error("expected a parsed timestamp")
	}

	// no-probe entry has nil Healthy (unknown).
	x, _ := c.GetStatusByHost("x.example.com")
	if x.Healthy != nil {
		t.Errorf("no-probe endpoint should be unknown (nil), got %v", *x.Healthy)
	}
}

func TestGatusNetworkFailureIsEmpty(t *testing.T) {
	c := NewGatusClient("http://127.0.0.1:0", WithGatusHTTPClient(&http.Client{Timeout: 100 * time.Millisecond}))
	if got := c.ListStatuses(); got != nil && len(got) != 0 {
		t.Errorf("network failure should yield empty, got %v", got)
	}
}

func TestGatusCachesWithinTTL(t *testing.T) {
	var hits int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		hits++
		_, _ = w.Write([]byte(`[{"name":"a","group":"g","url":"https://a/","results":[{"success":true,"timestamp":"2026-06-05T10:00:00Z"}]}]`))
	}))
	defer srv.Close()
	c := NewGatusClient(srv.URL, WithGatusHTTPClient(srv.Client()), WithGatusTTL(time.Hour))
	c.ListStatuses()
	c.ListStatuses()
	if hits != 1 {
		t.Errorf("expected 1 fetch within TTL, got %d", hits)
	}
	c.Invalidate()
	c.ListStatuses()
	if hits != 2 {
		t.Errorf("expected a refetch after Invalidate, got %d", hits)
	}
}

func TestHealthchecksListChecks(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Api-Key") != "k3y" {
			t.Errorf("missing/wrong api key: %q", r.Header.Get("X-Api-Key"))
		}
		_, _ = w.Write([]byte(`{"checks":[
			{"name":"daily","status":"UP","last_ping":"2026-06-05T03:00:00Z","n_pings":42},
			{"name":"mirror","status":"down","last_ping":"","n_pings":0}
		]}`))
	}))
	defer srv.Close()

	c := NewHealthchecksClient(srv.URL, "k3y", WithHealthchecksHTTPClient(srv.Client()))
	checks := c.ListChecks()
	if len(checks) != 2 {
		t.Fatalf("len = %d, want 2", len(checks))
	}
	if checks[0].Status != "up" {
		t.Errorf("status should be lowercased, got %q", checks[0].Status)
	}
	if checks[0].NPings != 42 {
		t.Errorf("NPings = %d, want 42", checks[0].NPings)
	}
}

func TestHealthchecksNoKeyNoFetch(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		t.Error("must not fetch without an api key")
	}))
	defer srv.Close()
	c := NewHealthchecksClient(srv.URL, "", WithHealthchecksHTTPClient(srv.Client()))
	if got := c.ListChecks(); len(got) != 0 {
		t.Errorf("no key should yield empty, got %v", got)
	}
}
