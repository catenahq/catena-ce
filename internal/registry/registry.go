// Package registry is the host-side store of mounted plugins. It holds
// the public plugin.Plugin contract and gates Business plugins on an
// active license. CE plugins register here directly; EE plugins register
// here after the shell loads them (license-gated) from catena-ee.
package registry

import (
	"sort"
	"sync"
	"time"

	"github.com/catenahq/catena-ce/license"
	"github.com/catenahq/catena-ce/plugin"
)

// Registry holds registered plugins, keyed by ID. Safe for concurrent
// registration so plugins can self-register from init hooks.
type Registry struct {
	mu      sync.RWMutex
	plugins map[string]plugin.Plugin
}

// New returns an empty registry.
func New() *Registry {
	return &Registry{plugins: make(map[string]plugin.Plugin)}
}

// Register adds p. Re-registering the same ID replaces it (last wins),
// matching an idempotent converge rather than erroring on a repeat.
func (r *Registry) Register(p plugin.Plugin) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.plugins[p.ID()] = p
}

// Enabled returns the plugins active for lic at now, sorted by ID for
// stable rendering. Community plugins are always enabled; Business plugins
// only while lic is Active within its grace window. A nil lic yields the
// CE-only set.
func (r *Registry) Enabled(lic *license.License, now time.Time, grace time.Duration) []plugin.Plugin {
	r.mu.RLock()
	defer r.mu.RUnlock()
	active := lic.Active(now, grace)
	out := make([]plugin.Plugin, 0, len(r.plugins))
	for _, p := range r.plugins {
		if p.Edition() == license.Business && !active {
			continue
		}
		out = append(out, p)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID() < out[j].ID() })
	return out
}
