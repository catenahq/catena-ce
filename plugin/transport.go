package plugin

// transport.go wraps the in-process Plugin contract in a hashicorp/go-plugin
// transport so EE plugins ship as separate binaries the catena-admin shell
// launches as child processes and talks to over net/rpc. The plugin binary
// calls Serve; the host (see ../loader) launches it and dispenses a Plugin
// whose methods are RPC calls. The interface the shell sees never changes:
// a loaded EE binary is just another plugin.Plugin.
//
// net/rpc (not gRPC) on purpose: the surface here is tiny (metadata + a
// Render string) so the protobuf/protoc toolchain would be pure overhead.
// The seam can move to gRPC later without touching plugin.Plugin or the
// registry, since both only ever see the interface.

import (
	"context"
	"net/rpc"

	goplugin "github.com/hashicorp/go-plugin"

	"github.com/catenahq/catena-ce/license"
)

// PluginKey is the name both sides dispense under in the go-plugin set.
const PluginKey = "catena_admin_plugin"

// Handshake gates a child process: a binary launched without the matching
// magic cookie (i.e. run by hand, not by the shell) prints a notice and
// exits instead of speaking the protocol. Bump ProtocolVersion on any
// breaking change to the RPC surface below.
var Handshake = goplugin.HandshakeConfig{
	ProtocolVersion:  1,
	MagicCookieKey:   "CATENA_ADMIN_PLUGIN",
	MagicCookieValue: "catena-admin-net-rpc-v1",
}

// PluginMeta carries a plugin's static identity across the wire so the host
// can read ID/Title/Edition without a round trip per call. The type and its
// fields must be exported: net/rpc only registers methods whose arg and
// reply types are exported or builtin. Edition rides as a plain string and
// is re-typed host-side.
type PluginMeta struct {
	ID      string
	Title   string
	Edition string
}

// rpcServer is the plugin-process side: it adapts net/rpc calls onto the
// real Plugin implementation.
type rpcServer struct{ impl Plugin }

func (s *rpcServer) Meta(_ struct{}, resp *PluginMeta) error {
	resp.ID = s.impl.ID()
	resp.Title = s.impl.Title()
	resp.Edition = string(s.impl.Edition())
	return nil
}

func (s *rpcServer) Render(_ struct{}, resp *string) error {
	out, err := s.impl.Render(context.Background())
	if err != nil {
		return err
	}
	*resp = out
	return nil
}

// rpcClient is the host side: it satisfies Plugin by issuing RPC calls to
// the child process. meta is fetched once at dispense time so the cheap
// accessors stay local.
type rpcClient struct {
	client *rpc.Client
	meta   PluginMeta
}

func (c *rpcClient) ID() string               { return c.meta.ID }
func (c *rpcClient) Title() string            { return c.meta.Title }
func (c *rpcClient) Edition() license.Edition { return license.Edition(c.meta.Edition) }

func (c *rpcClient) Render(_ context.Context) (string, error) {
	var out string
	if err := c.client.Call("Plugin.Render", struct{}{}, &out); err != nil {
		return "", err
	}
	return out, nil
}

// adminPlugin is the go-plugin adapter registered under PluginKey. impl is
// set on the plugin-process side (via Serve) and nil on the host side (the
// host only ever dispenses a client).
type adminPlugin struct{ impl Plugin }

func (p *adminPlugin) Server(*goplugin.MuxBroker) (interface{}, error) {
	return &rpcServer{impl: p.impl}, nil
}

func (*adminPlugin) Client(_ *goplugin.MuxBroker, c *rpc.Client) (interface{}, error) {
	cl := &rpcClient{client: c}
	if err := c.Call("Plugin.Meta", struct{}{}, &cl.meta); err != nil {
		return nil, err
	}
	return cl, nil
}

func pluginSet(impl Plugin) goplugin.PluginSet {
	return goplugin.PluginSet{PluginKey: &adminPlugin{impl: impl}}
}

// HostPluginSet is the plugin map the host loader passes to go-plugin; impl
// is nil because the host only dispenses clients, never serves.
func HostPluginSet() goplugin.PluginSet { return pluginSet(nil) }

// Serve runs p as a go-plugin binary. An EE plugin's main() is just
// plugin.Serve(thePlugin); it blocks, serving RPC to the launching shell.
func Serve(p Plugin) {
	goplugin.Serve(&goplugin.ServeConfig{
		HandshakeConfig: Handshake,
		Plugins:         pluginSet(p),
	})
}
