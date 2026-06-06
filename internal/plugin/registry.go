// Package plugin defines the catena-admin shell's panel/action plugin
// boundary -- the CE/EE seam.
//
// The public shell (this repo) always enables Community plugins. Business
// plugins are compiled separately in catena-enterprise, pulled onto a host
// only after the license endpoint validates the key, and enabled here only
// while a Business license is Active. One image: EE plugins ride in as
// downloaded binaries gated at runtime, never a second build of the shell.
package plugin

import (
	"sort"
	"sync"
	"time"

	"github.com/catenahq/catena/internal/license"
)

// Plugin is a unit the shell can mount: an admin panel plus its actions.
// Concrete panels (audit, access governance, managed-update controls)
// implement this; the shell only ever sees the interface.
type Plugin interface {
	ID() string                // stable kebab-case identifier
	Title() string             // human label for the nav
	Edition() license.Edition  // Community or Business
}

// Registry holds registered plugins. Safe for concurrent registration so
// plugins can self-register from init-time hooks.
type Registry struct {
	mu      sync.RWMutex
	plugins map[string]Plugin
}

// NewRegistry returns an empty registry.
func NewRegistry() *Registry {
	return &Registry{plugins: make(map[string]Plugin)}
}

// Register adds p. Re-registering the same ID replaces it (last wins),
// matching an idempotent converge rather than erroring on a repeat.
func (r *Registry) Register(p Plugin) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.plugins[p.ID()] = p
}

// Enabled returns the plugins active for lic at now, sorted by ID for
// stable rendering. Community plugins are always enabled; Business plugins
// only when lic is Active within its grace window. A nil lic yields the
// CE-only set.
func (r *Registry) Enabled(lic *license.License, now time.Time, grace time.Duration) []Plugin {
	r.mu.RLock()
	defer r.mu.RUnlock()
	active := lic.Active(now, grace)
	out := make([]Plugin, 0, len(r.plugins))
	for _, p := range r.plugins {
		if p.Edition() == license.Business && !active {
			continue
		}
		out = append(out, p)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID() < out[j].ID() })
	return out
}
