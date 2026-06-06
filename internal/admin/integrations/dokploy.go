package integrations

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

var errBadStatus = errors.New("integrations: non-2xx status")

// Domain is one app domain (host + port).
type Domain struct {
	Host string
	Port int
}

// DokployItem normalizes a Dokploy application or compose to one shape. Kind is
// "application" or "compose"; ComposeBody is the YAML for composes, empty for
// applications.
type DokployItem struct {
	ProjectName string
	Kind        string
	ItemID      string
	AppName     string
	Description string
	Domains     []Domain
	ComposeBody string
}

// DokployClient is a read-only Dokploy API client (project.all + compose.one +
// domain.by*). TTL-cached and fail-soft. Safe for concurrent use.
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

type dokployProject struct {
	Name         string `json:"name"`
	Environments []struct {
		Applications []map[string]any `json:"applications"`
		Compose      []map[string]any `json:"compose"`
	} `json:"environments"`
}

func (c *DokployClient) fetchAll() []DokployItem {
	var projects []dokployProject
	if err := c.get("/api/project.all", nil, &projects); err != nil {
		// Empty so the UI shows "no apps" rather than 500; the System tab is
		// where the operator sees the Dokploy probe is red.
		return nil
	}
	var out []DokployItem
	for _, proj := range projects {
		if len(proj.Environments) == 0 {
			continue
		}
		env := proj.Environments[0]
		for _, app := range env.Applications {
			if item, ok := c.fetchApplication(proj.Name, app); ok {
				out = append(out, item)
			}
		}
		for _, comp := range env.Compose {
			if item, ok := c.fetchCompose(proj.Name, comp); ok {
				out = append(out, item)
			}
		}
	}
	return out
}

func (c *DokployClient) fetchApplication(project string, app map[string]any) (DokployItem, bool) {
	id := mapStr(app, "applicationId")
	if id == "" {
		return DokployItem{}, false
	}
	return DokployItem{
		ProjectName: project,
		Kind:        "application",
		ItemID:      id,
		AppName:     firstNonEmpty(mapStr(app, "appName"), mapStr(app, "name")),
		Description: mapStr(app, "description"),
		Domains:     c.fetchDomains("/api/domain.byApplicationId", "applicationId", id),
	}, true
}

func (c *DokployClient) fetchCompose(project string, comp map[string]any) (DokployItem, bool) {
	id := mapStr(comp, "composeId")
	if id == "" {
		return DokployItem{}, false
	}
	var detail struct {
		ComposeFile string `json:"composeFile"`
	}
	_ = c.get("/api/compose.one", url.Values{"composeId": {id}}, &detail)
	return DokployItem{
		ProjectName: project,
		Kind:        "compose",
		ItemID:      id,
		AppName:     firstNonEmpty(mapStr(comp, "appName"), mapStr(comp, "name")),
		Description: mapStr(comp, "description"),
		Domains:     c.fetchDomains("/api/domain.byComposeId", "composeId", id),
		ComposeBody: detail.ComposeFile,
	}, true
}

func (c *DokployClient) fetchDomains(path, idParam, id string) []Domain {
	var data []struct {
		Host string `json:"host"`
		Port int    `json:"port"`
	}
	if err := c.get(path, url.Values{idParam: {id}}, &data); err != nil {
		return nil
	}
	var out []Domain
	for _, d := range data {
		if d.Host == "" {
			continue
		}
		port := d.Port
		if port == 0 {
			port = 80
		}
		out = append(out, Domain{Host: d.Host, Port: port})
	}
	return out
}

// get issues a GET to path with query params and decodes the JSON into out.
func (c *DokployClient) get(path string, params url.Values, out any) error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	u := c.baseURL + path
	if len(params) > 0 {
		u += "?" + params.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return err
	}
	req.Header.Set("x-api-key", c.apiKey)
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

func mapStr(m map[string]any, k string) string {
	if s, ok := m[k].(string); ok {
		return s
	}
	return ""
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}
