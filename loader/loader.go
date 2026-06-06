// Package loader launches an EE plugin binary as a go-plugin child process
// and hands back the dispensed plugin.Plugin. The catena-admin shell calls
// Load for each binary it pulled (license-gated) into its plugins dir; the
// returned plugin behaves exactly like an in-process one, so the registry
// gates it on the active license the same way.
//
// Loading is itself meant to run only behind an active license: the shell
// does not spawn EE binaries at all when CE-only. The registry's edition
// check is the second line of defence, not the first.
package loader

import (
	"fmt"
	"io"
	"os/exec"

	hclog "github.com/hashicorp/go-hclog"
	goplugin "github.com/hashicorp/go-plugin"

	"github.com/catenahq/catena-ce/plugin"
)

// Load launches the plugin binary at path, performs the handshake, and
// dispenses its plugin.Plugin. The returned close function terminates the
// child process; call it on shell shutdown or when dropping the plugin.
// On any failure the child is killed before returning.
func Load(path string) (plugin.Plugin, func(), error) {
	client := goplugin.NewClient(&goplugin.ClientConfig{
		HandshakeConfig:  plugin.Handshake,
		Plugins:          plugin.HostPluginSet(),
		Cmd:              exec.Command(path),
		AllowedProtocols: []goplugin.Protocol{goplugin.ProtocolNetRPC},
		// Quiet: go-plugin's default logger is chatty on stderr. Surface
		// only real errors; the shell's own log covers load outcomes.
		Logger: hclog.New(&hclog.LoggerOptions{
			Name:   "plugin-loader",
			Level:  hclog.Error,
			Output: io.Discard,
		}),
	})

	rpcClient, err := client.Client()
	if err != nil {
		client.Kill()
		return nil, nil, fmt.Errorf("loader: connect %s: %w", path, err)
	}

	raw, err := rpcClient.Dispense(plugin.PluginKey)
	if err != nil {
		client.Kill()
		return nil, nil, fmt.Errorf("loader: dispense %s: %w", path, err)
	}

	p, ok := raw.(plugin.Plugin)
	if !ok {
		client.Kill()
		return nil, nil, fmt.Errorf("loader: %s does not implement plugin.Plugin (got %T)", path, raw)
	}

	return p, client.Kill, nil
}
