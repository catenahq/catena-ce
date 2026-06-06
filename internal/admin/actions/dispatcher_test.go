package actions

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"testing"
)

type fakeRunner struct {
	stdout []string
	stderr []string
	rc     int
	err    error
	gotCmd string
	gotEnv map[string]string
}

func (f *fakeRunner) Run(_ context.Context, command string, env map[string]string, onStdout, onStderr func(string)) (int, error) {
	f.gotCmd = command
	f.gotEnv = env
	for _, l := range f.stdout {
		onStdout(l)
	}
	for _, l := range f.stderr {
		onStderr(l)
	}
	return f.rc, f.err
}

func TestStreamDispatchHappyPath(t *testing.T) {
	var buf bytes.Buffer
	fr := &fakeRunner{stdout: []string{"line1", "line2"}, stderr: []string{"warn"}, rc: 0}
	rc := StreamDispatch(&buf, nil, fr, "backup-now", "arg1", "op@x")
	if rc != 0 {
		t.Fatalf("rc = %d, want 0", rc)
	}
	out := buf.String()
	for _, want := range []string{
		"data: action=backup-now email=op@x",
		"data: line1\n\n",
		"data: line2\n\n",
		"event: stderr\ndata: warn\n\n",
		"event: end\ndata: 0\n\n",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in:\n%s", want, out)
		}
	}
	// Command + forwarded email env reach the runner.
	if fr.gotCmd != "backup-now arg1" {
		t.Errorf("command = %q, want 'backup-now arg1'", fr.gotCmd)
	}
	if fr.gotEnv["X_FORWARDED_EMAIL"] != "op@x" {
		t.Errorf("X_FORWARDED_EMAIL = %q, want op@x", fr.gotEnv["X_FORWARDED_EMAIL"])
	}
}

func TestStreamDispatchNilRunner(t *testing.T) {
	var buf bytes.Buffer
	rc := StreamDispatch(&buf, nil, nil, "x", "", "")
	if rc != -1 {
		t.Fatalf("rc = %d, want -1", rc)
	}
	if !strings.Contains(buf.String(), "no dispatcher configured") {
		t.Errorf("expected a no-dispatcher error frame, got %q", buf.String())
	}
}

func TestStreamDispatchRunnerError(t *testing.T) {
	var buf bytes.Buffer
	rc := StreamDispatch(&buf, nil, &fakeRunner{err: errors.New("boom")}, "x", "", "")
	if rc != -1 {
		t.Fatalf("rc = %d, want -1", rc)
	}
	out := buf.String()
	if !strings.Contains(out, "dispatch error: boom") || !strings.Contains(out, "event: end\ndata: -1") {
		t.Errorf("expected error + end -1 frames, got %q", out)
	}
}

func TestConfigFromEnvDefaults(t *testing.T) {
	t.Setenv("CATENA_ADMIN_SSH_HOST", "")
	t.Setenv("ADMIN_SSH_HOST", "")
	t.Setenv("CATENA_ADMIN_SSH_USER", "")
	t.Setenv("ADMIN_SSH_USER", "")
	cfg := ConfigFromEnv()
	if cfg.Host != "host.docker.internal" || cfg.User != "catena-admin-runner" || cfg.Port != 22 {
		t.Errorf("defaults wrong: %+v", cfg)
	}
}
