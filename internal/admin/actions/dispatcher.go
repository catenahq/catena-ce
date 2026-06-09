package actions

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
)

// Runner executes a single dispatched command, delivering stdout/stderr to the
// callbacks line by line and returning the exit code. The SSH implementation
// is the production path; tests inject a fake to drive a deterministic
// transcript without a real host. This is the single seam every dispatched
// action funnels through -- CE actions over SSH today, EE plugin actions over
// the go-plugin RPC later -- so the host SSH key lives in exactly one place.
type Runner interface {
	Run(ctx context.Context, command string, env map[string]string, onStdout, onStderr func(string)) (int, error)
}

// StreamDispatch runs actionName (with payload) through runner, writing SSE
// frames to w and flushing after each. Returns the final exit code (-1 on a
// dispatch error). A nil runner emits an error frame rather than panicking.
func StreamDispatch(w io.Writer, flush func(), runner Runner, actionName, payload, email string) int {
	write := func(s string) {
		_, _ = io.WriteString(w, s)
		if flush != nil {
			flush()
		}
	}
	write(FormatStart(actionName, email))
	if runner == nil {
		write(FormatStderr("no dispatcher configured"))
		write(FormatEnd(-1, "no dispatcher configured"))
		return -1
	}
	env := map[string]string{}
	if email != "" {
		// Forwarded so audit-bearing host actions record who clicked. The
		// host sshd must list X_FORWARDED_EMAIL under AcceptEnv.
		env["X_FORWARDED_EMAIL"] = email
	}
	rc, err := runner.Run(
		context.Background(),
		BuildCommand(actionName, payload),
		env,
		func(line string) { write(FormatStdout(line)) },
		func(line string) { write(FormatStderr(line)) },
	)
	if err != nil {
		write(FormatStderr("dispatch error: " + err.Error()))
		write(FormatEnd(-1, err.Error()))
		return -1
	}
	write(FormatEnd(rc, ""))
	return rc
}

// SSHConfig is the runtime SSH parameters resolved from the environment +
// role-installed files. Defaults match the catena-admin Ansible role's
// host-side layout.
type SSHConfig struct {
	Host           string
	User           string
	KeyPath        string
	KnownHostsPath string
	Port           int
	ConnectTimeout time.Duration
}

// ConfigFromEnv builds an SSHConfig from CATENA_ADMIN_SSH_* (with the older
// ADMIN_SSH_* fallbacks the compose file provides).
func ConfigFromEnv() SSHConfig {
	return SSHConfig{
		Host:           firstNonEmpty(os.Getenv("CATENA_ADMIN_SSH_HOST"), envOr("ADMIN_SSH_HOST", "host.docker.internal")),
		User:           firstNonEmpty(os.Getenv("CATENA_ADMIN_SSH_USER"), envOr("ADMIN_SSH_USER", "catena-admin-runner")),
		KeyPath:        firstNonEmpty(os.Getenv("CATENA_ADMIN_SSH_KEY_PATH"), envOr("ADMIN_SSH_KEY_PATH", "/etc/catena/admin-ssh/id_ed25519")),
		KnownHostsPath: envOr("CATENA_ADMIN_SSH_KNOWN_HOSTS", "/etc/catena/admin-ssh/known_hosts"),
		Port:           atoiOr(os.Getenv("CATENA_ADMIN_SSH_PORT"), 22),
		ConnectTimeout: time.Duration(atoiOr(os.Getenv("CATENA_ADMIN_SSH_CONNECT_TIMEOUT"), 10)) * time.Second,
	}
}

// SSHRunner dispatches over a paramiko-equivalent x/crypto/ssh channel to the
// host runner account, whose forced-command interprets the whole command as
// $SSH_ORIGINAL_COMMAND.
type SSHRunner struct{ Cfg SSHConfig }

// NewSSHRunner builds an SSHRunner from the environment.
func NewSSHRunner() SSHRunner { return SSHRunner{Cfg: ConfigFromEnv()} }

// Run opens an SSH session, sets env, executes command, and streams stdout +
// stderr line by line, returning the remote exit code.
func (s SSHRunner) Run(ctx context.Context, command string, env map[string]string, onStdout, onStderr func(string)) (int, error) {
	hostKeys, err := knownhosts.New(s.Cfg.KnownHostsPath)
	if err != nil {
		return -1, fmt.Errorf("known_hosts load failed: %w", err)
	}
	keyBytes, err := os.ReadFile(s.Cfg.KeyPath)
	if err != nil {
		return -1, fmt.Errorf("read key: %w", err)
	}
	signer, err := ssh.ParsePrivateKey(keyBytes)
	if err != nil {
		return -1, fmt.Errorf("parse key: %w", err)
	}
	cfg := &ssh.ClientConfig{
		User:            s.Cfg.User,
		Auth:            []ssh.AuthMethod{ssh.PublicKeys(signer)},
		HostKeyCallback: hostKeys,
		// Pin host-key negotiation to ed25519. The catena-admin role scans
		// the host with `ssh-keyscan -t ed25519` and writes an ed25519-only
		// known_hosts, but Debian sshd also serves rsa/ecdsa host keys.
		// Without this pin x/crypto may negotiate one of those, which the
		// ed25519-only known_hosts cannot match -> "knownhosts: key
		// mismatch" on every dispatch. Keep in lockstep with the role's
		// keyscan key type.
		HostKeyAlgorithms: []string{ssh.KeyAlgoED25519},
		Timeout:           s.Cfg.ConnectTimeout,
	}
	addr := net.JoinHostPort(s.Cfg.Host, strconv.Itoa(s.Cfg.Port))
	client, err := ssh.Dial("tcp", addr, cfg)
	if err != nil {
		return -1, fmt.Errorf("ssh connect failed: %w", err)
	}
	defer client.Close()

	session, err := client.NewSession()
	if err != nil {
		return -1, fmt.Errorf("open session failed: %w", err)
	}
	defer session.Close()

	for k, v := range env {
		// A server without the env in AcceptEnv refuses it; the action still
		// runs (the audit row just lacks attribution), so ignore the error.
		_ = session.Setenv(k, v)
	}

	stdout, err := session.StdoutPipe()
	if err != nil {
		return -1, err
	}
	stderr, err := session.StderrPipe()
	if err != nil {
		return -1, err
	}
	if err := session.Start(command); err != nil {
		return -1, fmt.Errorf("exec failed: %w", err)
	}

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); scanLines(stdout, onStdout) }()
	go func() { defer wg.Done(); scanLines(stderr, onStderr) }()
	wg.Wait()

	if err := session.Wait(); err != nil {
		var exitErr *ssh.ExitError
		if asExitError(err, &exitErr) {
			return exitErr.ExitStatus(), nil
		}
		return -1, err
	}
	return 0, nil
}

func scanLines(r io.Reader, onLine func(string)) {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for sc.Scan() {
		onLine(sc.Text())
	}
}

func asExitError(err error, target **ssh.ExitError) bool {
	if e, ok := err.(*ssh.ExitError); ok {
		*target = e
		return true
	}
	return false
}

func atoiOr(s string, def int) int {
	if n, err := strconv.Atoi(strings.TrimSpace(s)); err == nil {
		return n
	}
	return def
}
