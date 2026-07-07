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

func TestPortainerListItems(t *testing.T) {
	// Portainer stack API: list stacks + fetch each stack's compose file; the
	// tile host comes from the vps.route.host label. An inactive stack and a
	// stack with no vps.route.host must both be skipped.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-API-Key") != "dk" {
			t.Errorf("missing api key")
		}
		switch r.URL.Path {
		case "/api/stacks":
			_, _ = w.Write([]byte(`[
				{"Id":1,"Name":"Nextcloud","Status":1},
				{"Id":2,"Name":"stopped","Status":2},
				{"Id":3,"Name":"no-route","Status":1}
			]`))
		case "/api/stacks/1/file":
			_, _ = w.Write([]byte(`{"StackFileContent":"services:\n  app:\n    labels:\n      vps.route.host: cloud.example.com\n      vps.route.port: 443\n      vps.auth.mode: public\n"}`))
		case "/api/stacks/3/file":
			_, _ = w.Write([]byte(`{"StackFileContent":"services:\n  app:\n    image: x\n"}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	c := NewPortainerClient(srv.URL, "dk", WithPortainerHTTPClient(srv.Client()))
	items := c.ListItems(false)
	if len(items) != 1 {
		t.Fatalf("ListItems = %d, want 1 (active + routed only)", len(items))
	}
	it := items[0]
	if it.Kind != "compose" || it.ItemID != "1" || it.AppName != "Nextcloud" {
		t.Errorf("item = %+v", it)
	}
	if len(it.Domains) != 1 || it.Domains[0].Host != "cloud.example.com" || it.Domains[0].Port != 443 {
		t.Errorf("domains = %+v", it.Domains)
	}
	if !contains(it.ComposeBody, "vps.auth.mode: public") {
		t.Errorf("compose body not fetched: %q", it.ComposeBody)
	}
}

func TestPortainerBaseURLNormalizesApiSuffix(t *testing.T) {
	c := NewPortainerClient("http://dok:9000/api/", "k")
	if c.baseURL != "http://dok:9000" {
		t.Errorf("baseURL = %q, want http://dok:9000", c.baseURL)
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (s == sub || indexOf(s, sub) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
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
