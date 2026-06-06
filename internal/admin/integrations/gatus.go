// Package integrations holds the read-only HTTP clients the catena-admin
// shell reads app + infra status from (Gatus, Healthchecks, Dokploy, ...).
// Each is TTL-cached and fails soft: a network error yields an empty result
// so a page renders an "unknown" state rather than crashing. Ported from the
// Python catena_admin.integrations.
package integrations

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"sync"
	"time"
)

// EndpointStatus is one Gatus-probed endpoint's latest outcome. Healthy is nil
// when no recent probe is recorded (rendered as an "unknown" dot).
type EndpointStatus struct {
	Name        string
	Group       string
	Healthy     *bool
	LastCheckTS *float64 // epoch seconds; nil when no probe recorded
	URL         string
}

// GatusClient reads /api/v1/endpoints/statuses. Safe for concurrent use.
type GatusClient struct {
	baseURL string
	ttl     time.Duration
	hc      *http.Client

	mu        sync.Mutex
	statuses  []EndpointStatus
	byHost    map[string]EndpointStatus
	fetchedAt time.Time
}

// GatusOption configures a GatusClient.
type GatusOption func(*GatusClient)

// WithGatusHTTPClient injects an http.Client (tests point it at httptest).
func WithGatusHTTPClient(hc *http.Client) GatusOption {
	return func(c *GatusClient) { c.hc = hc }
}

// WithGatusTTL overrides the 30s cache TTL.
func WithGatusTTL(d time.Duration) GatusOption {
	return func(c *GatusClient) { c.ttl = d }
}

// NewGatusClient builds a client for baseURL (trailing slash trimmed).
func NewGatusClient(baseURL string, opts ...GatusOption) *GatusClient {
	c := &GatusClient{
		baseURL: strings.TrimRight(baseURL, "/"),
		ttl:     30 * time.Second,
		hc:      &http.Client{Timeout: 5 * time.Second},
		byHost:  map[string]EndpointStatus{},
	}
	for _, o := range opts {
		o(c)
	}
	return c
}

// GetStatusByHost returns the status whose URL host contains host, used by the
// Apps tab to render per-tile dots without callers knowing Gatus naming.
func (c *GatusClient) GetStatusByHost(host string) (EndpointStatus, bool) {
	c.refreshIfStale()
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.byHost[host]
	return s, ok
}

// ListStatuses returns a copy of the cached endpoint statuses.
func (c *GatusClient) ListStatuses() []EndpointStatus {
	c.refreshIfStale()
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]EndpointStatus(nil), c.statuses...)
}

// Invalidate drops the cache so the next read refetches.
func (c *GatusClient) Invalidate() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.statuses, c.byHost, c.fetchedAt = nil, map[string]EndpointStatus{}, time.Time{}
}

func (c *GatusClient) refreshIfStale() {
	c.mu.Lock()
	fresh := len(c.statuses) > 0 && time.Since(c.fetchedAt) < c.ttl
	c.mu.Unlock()
	if fresh {
		return
	}
	statuses := c.fetch()
	c.mu.Lock()
	c.statuses = statuses
	c.byHost = indexByHost(statuses)
	c.fetchedAt = time.Now()
	c.mu.Unlock()
}

type gatusEntry struct {
	Name    string        `json:"name"`
	Group   string        `json:"group"`
	URL     string        `json:"url"`
	Results []gatusResult `json:"results"`
}

type gatusResult struct {
	Success   bool            `json:"success"`
	Timestamp json.RawMessage `json:"timestamp"`
}

func (c *GatusClient) fetch() []EndpointStatus {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/api/v1/endpoints/statuses", nil)
	if err != nil {
		return nil
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil
	}
	var data []gatusEntry
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return nil
	}
	out := make([]EndpointStatus, 0, len(data))
	for _, e := range data {
		s := EndpointStatus{Name: e.Name, Group: e.Group, URL: e.URL}
		if len(e.Results) > 0 {
			latest := e.Results[0]
			healthy := latest.Success
			s.Healthy = &healthy
			s.LastCheckTS = parseTS(latest.Timestamp)
		}
		out = append(out, s)
	}
	return out
}

// indexByHost maps host -> status. When a host appears in multiple endpoints
// (internal alias + public 302-as-up for the same app), prefer the entry whose
// name suggests the PUBLIC check; else keep the first seen.
func indexByHost(statuses []EndpointStatus) map[string]EndpointStatus {
	score := map[string]int{"public": 3, "302": 2, "external": 2, "internal": -1, "alias": -1}
	out := map[string]EndpointStatus{}
	best := map[string]int{}
	for _, s := range statuses {
		host := hostOf(s.URL)
		if host == "" {
			continue
		}
		rank := 0
		lower := strings.ToLower(s.Name)
		for k, v := range score {
			if strings.Contains(lower, k) && v > rank {
				rank = v
			}
		}
		if _, seen := out[host]; !seen || rank > best[host] {
			out[host] = s
			best[host] = rank
		}
	}
	return out
}

func hostOf(url string) string {
	if url == "" {
		return ""
	}
	if i := strings.Index(url, "://"); i >= 0 {
		url = url[i+3:]
	}
	url = strings.SplitN(url, "/", 2)[0]
	return strings.SplitN(url, ":", 2)[0]
}

// parseTS tolerates Gatus's RFC3339 string timestamps and numeric epochs,
// returning epoch seconds. nil on absent/unparseable.
func parseTS(raw json.RawMessage) *float64 {
	if len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var num float64
	if err := json.Unmarshal(raw, &num); err == nil {
		return &num
	}
	var s string
	if err := json.Unmarshal(raw, &s); err != nil || s == "" {
		return nil
	}
	t, err := time.Parse(time.RFC3339, strings.Replace(s, "Z", "+00:00", 1))
	if err != nil {
		// Try the RFC3339 form with an explicit Z too.
		if t, err = time.Parse(time.RFC3339, s); err != nil {
			return nil
		}
	}
	secs := float64(t.Unix())
	return &secs
}
