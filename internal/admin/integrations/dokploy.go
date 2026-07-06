package integrations

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/catenahq/catena-ce/internal/admin/labels"
)

var errBadStatus = errors.New("integrations: non-2xx status")

// Domain is one app ingress (host + port). Under the Dokploy->Portainer
// migration it comes from the compose vps.route.host label, not a domain API.
type Domain struct {
	Host string
	Port int
}

// DokployItem normalizes a Portainer stack to the shape the Apps tab consumes.
// Kind is always "compose" (Portainer stacks are compose); ComposeBody is the
// stack's StackFileContent. Name/ItemID map to the stack Name/Id. (Type name
// kept during the migration; renamed in Phase 5.)
type DokployItem struct {
	ProjectName string
	Kind        string
	ItemID      string
	AppName     string
	Description string
	Domains     []Domain
	ComposeBody string
}

// DokployClient is a read-only Portainer stack API client (GET /api/stacks +
// /api/stacks/{id}/file). TTL-cached and fail-soft. Safe for concurrent use.
type DokployClient struct {
	baseURL string
	apiKey  string
	ttl     time.Duration
	hc      *http.Client

	mu        sync.Mutex
	items     []DokployItem
	fetchedAt time.Time
}

// DokployOption configures a DokployClient.
type DokployOption func(*DokployClient)

// WithDokployHTTPClient injects an http.Client (tests point it at httptest).
func WithDokployHTTPClient(hc *http.Client) DokployOption {
	return func(c *DokployClient) { c.hc = hc }
}

// WithDokployTTL overrides the 30s cache TTL.
func WithDokployTTL(d time.Duration) DokployOption {
	return func(c *DokployClient) { c.ttl = d }
}

// NewDokployClient builds a client. Callers may pass a base URL ending in /api
// (dashboard-sync style) or not; both normalize to bare + a prepended /api.
func NewDokployClient(baseURL, apiKey string, opts ...DokployOption) *DokployClient {
	base := strings.TrimRight(baseURL, "/")
	base = strings.TrimSuffix(base, "/api")
	c := &DokployClient{
		baseURL: base,
		apiKey:  apiKey,
		ttl:     30 * time.Second,
		hc:      &http.Client{Timeout: 10 * time.Second},
	}
	for _, o := range opts {
		o(c)
	}
	return c
}

// ListItems returns every deployed application + compose across all projects,
// cached for the TTL. A refresh on a stale cache is serialized so a thundering
// herd hits Dokploy at most once.
func (c *DokployClient) ListItems(forceRefresh bool) []DokployItem {
	c.mu.Lock()
	if !forceRefresh && len(c.items) > 0 && time.Since(c.fetchedAt) < c.ttl {
		out := append([]DokployItem(nil), c.items...)
		c.mu.Unlock()
		return out
	}
	// Hold the lock across the fetch so concurrent callers wait for the one
	// refresh rather than each firing their own fan-out.
	defer c.mu.Unlock()
	if !forceRefresh && len(c.items) > 0 && time.Since(c.fetchedAt) < c.ttl {
		return append([]DokployItem(nil), c.items...)
	}
	c.items = c.fetchAll()
	c.fetchedAt = time.Now()
	return append([]DokployItem(nil), c.items...)
}

// Invalidate drops the cache so the next render refetches (called after a
// mutation).
func (c *DokployClient) Invalidate() {
	c.mu.Lock()
	c.items, c.fetchedAt = nil, time.Time{}
	c.mu.Unlock()
}

type portainerStack struct {
	ID     int    `json:"Id"`
	Name   string `json:"Name"`
	Status int    `json:"Status"` // 1 = active, 2 = inactive
}

func (c *DokployClient) fetchAll() []DokployItem {
	var stacks []portainerStack
	if err := c.get("/api/stacks", &stacks); err != nil {
		// Empty so the UI shows "no apps" rather than 500; the System tab is
		// where the operator sees the Portainer probe is red.
		return nil
	}
	var out []DokployItem
	for _, st := range stacks {
		if st.Name == "" || st.Status != 1 {
			continue // inactive / never-deployed -> no live backend to tile
		}
		body := c.stackFile(st.ID)
		route := labels.ExtractRouteLabels(body)
		if route.Host == "" {
			continue // no public host declared -> not a tile
		}
		out = append(out, DokployItem{
			ProjectName: "", // Portainer has no project grouping
			Kind:        "compose",
			ItemID:      strconv.Itoa(st.ID),
			AppName:     st.Name,
			Domains:     []Domain{{Host: route.Host, Port: route.Port}},
			ComposeBody: body,
		})
	}
	return out
}

// stackFile fetches a stack's StackFileContent (the compose body). Fail-soft:
// a fetch error yields an empty body, so the caller skips a stack with no
// readable route label rather than 500ing the whole grid.
func (c *DokployClient) stackFile(id int) string {
	var detail struct {
		StackFileContent string `json:"StackFileContent"`
	}
	if err := c.get("/api/stacks/"+strconv.Itoa(id)+"/file", &detail); err != nil {
		return ""
	}
	return detail.StackFileContent
}

// get issues a GET to path and decodes the JSON into out.
func (c *DokployClient) get(path string, out any) error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("X-API-Key", c.apiKey)
	req.Header.Set("Accept", "application/json")
	resp, err := c.hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return errBadStatus
	}
	return json.NewDecoder(resp.Body).Decode(out)
}
