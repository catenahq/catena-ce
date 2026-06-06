package integrations

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"sync"
	"time"
)

// Check is one self-hosted Healthchecks check's current state.
type Check struct {
	Name     string
	Status   string // up | down | late | paused | new | unknown
	LastPing string // RFC3339 from Healthchecks; empty if never pinged
	NPings   int
}

// HealthchecksClient reads GET /api/v3/checks/ with X-Api-Key auth. Safe for
// concurrent use; fails soft to an empty list.
type HealthchecksClient struct {
	baseURL string
	apiKey  string
	ttl     time.Duration
	hc      *http.Client

	mu        sync.Mutex
	checks    []Check
	fetchedAt time.Time
}

// HealthchecksOption configures a HealthchecksClient.
type HealthchecksOption func(*HealthchecksClient)

// WithHealthchecksHTTPClient injects an http.Client (tests point it at httptest).
func WithHealthchecksHTTPClient(hc *http.Client) HealthchecksOption {
	return func(c *HealthchecksClient) { c.hc = hc }
}

// WithHealthchecksTTL overrides the 30s cache TTL.
func WithHealthchecksTTL(d time.Duration) HealthchecksOption {
	return func(c *HealthchecksClient) { c.ttl = d }
}

// NewHealthchecksClient builds a client for baseURL with the read-only api key.
func NewHealthchecksClient(baseURL, apiKey string, opts ...HealthchecksOption) *HealthchecksClient {
	c := &HealthchecksClient{
		baseURL: strings.TrimRight(baseURL, "/"),
		apiKey:  apiKey,
		ttl:     30 * time.Second,
		hc:      &http.Client{Timeout: 5 * time.Second},
	}
	for _, o := range opts {
		o(c)
	}
	return c
}

// ListChecks returns a copy of the cached checks, refetching when stale.
func (c *HealthchecksClient) ListChecks() []Check {
	c.mu.Lock()
	if len(c.checks) > 0 && time.Since(c.fetchedAt) < c.ttl {
		out := append([]Check(nil), c.checks...)
		c.mu.Unlock()
		return out
	}
	c.mu.Unlock()

	fetched := c.fetch()
	c.mu.Lock()
	c.checks = fetched
	c.fetchedAt = time.Now()
	out := append([]Check(nil), c.checks...)
	c.mu.Unlock()
	return out
}

// Invalidate drops the cache so the next read refetches.
func (c *HealthchecksClient) Invalidate() {
	c.mu.Lock()
	c.checks, c.fetchedAt = nil, time.Time{}
	c.mu.Unlock()
}

type hcResponse struct {
	Checks []hcCheck `json:"checks"`
}

type hcCheck struct {
	Name     string `json:"name"`
	Status   string `json:"status"`
	LastPing string `json:"last_ping"`
	NPings   int    `json:"n_pings"`
}

func (c *HealthchecksClient) fetch() []Check {
	if c.apiKey == "" {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/api/v3/checks/", nil)
	if err != nil {
		return nil
	}
	req.Header.Set("X-Api-Key", c.apiKey)
	req.Header.Set("Accept", "application/json")
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil
	}
	var data hcResponse
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return nil
	}
	out := make([]Check, 0, len(data.Checks))
	for _, ch := range data.Checks {
		status := strings.ToLower(ch.Status)
		if status == "" {
			status = "unknown"
		}
		out = append(out, Check{Name: ch.Name, Status: status, LastPing: ch.LastPing, NPings: ch.NPings})
	}
	return out
}
