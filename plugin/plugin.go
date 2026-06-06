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
	// Actions returns the action buttons this plugin contributes to the
	// shell's Actions/Recovery catalog. Static metadata (the merge happens
	// in the shell); empty is fine. Plugin actions DISPATCH through the
	// shell's own SSH runner -- a plugin never holds the host key; the Shell
	// command runs on the host (typically invoking the plugin's downloaded
	// EE binary).
	Actions() []ActionSpec
}

// ActionSpec is one action button a plugin contributes. It mirrors the
// shell's catalog row but lives in the public SDK so it crosses the plugin
// transport; the shell maps it onto its internal Action type (stamping the
// plugin id as the source). All fields are exported for net/rpc.
type ActionSpec struct {
	Name      string // stable dispatch key (also the host dispatcher key)
	Title     string
	TitleFR   string
	Category  string // Upgrades | Backups | Initial apps setup | Ops | Recovery
	Icon      string
	Timeout   int // seconds; 0 = shell default
	Shell     string
	Arguments []ArgSpec
}

// ArgSpec declares one argument prompt on an action (e.g. a passphrase).
type ArgSpec struct {
	Name string
	Type string // text | password | select | ...
}
