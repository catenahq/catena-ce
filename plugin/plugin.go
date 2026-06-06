// Package plugin is the public SDK that enterprise (Business) plugins
// implement. It lives outside internal/ on purpose: the catena-ee module
// imports it to build license-gated plugins, which Go's internal-package
// rule would otherwise forbid.
//
// For now this is the in-process contract. The go-plugin / gRPC transport
// (so EE plugins ship as separately-downloaded binaries the shell launches
// at runtime) wraps this same interface in a following slice.
package plugin

import (
	"context"

	"github.com/catenahq/catena-ce/license"
)

// Plugin is a unit the catena-admin shell mounts: an admin panel plus its
// actions. Community plugins ship in catena-ce; Business plugins ship in
// catena-ee and are enabled only while a license is active.
type Plugin interface {
	// ID is a stable kebab-case identifier (also the allow-list key).
	ID() string
	// Title is the human label shown in the shell nav.
	Title() string
	// Edition is Community or Business; the shell gates Business plugins
	// on an active license.
	Edition() license.Edition
	// Render returns the panel body for the current request. Kept minimal
	// here; richer request/response types arrive with the transport slice.
	Render(ctx context.Context) (string, error)
}
