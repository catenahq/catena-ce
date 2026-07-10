package loader

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// The happy path (a real go-plugin child) is exercised by the catena-ee
// transport integration test and the bench's activate_ee plugin pull; what
// the unit level can pin hermetically is the failure contract: every error
// path returns a wrapped error, a nil plugin, and never leaks a child.

func TestLoadNonexistentPathErrors(t *testing.T) {
	p, closeFn, err := Load(filepath.Join(t.TempDir(), "no-such-binary"))
	if err == nil {
		if closeFn != nil {
			closeFn()
		}
		t.Fatal("expected an error for a nonexistent binary")
	}
	if p != nil || closeFn != nil {
		t.Fatalf("failure must return nil plugin + nil close, got %v close=%t", p, closeFn != nil)
	}
}

func TestLoadNonPluginBinaryErrors(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sh stub is POSIX-only")
	}
	// An executable that exits immediately never completes the go-plugin
	// handshake; Load must surface a connect error, not hang.
	stub := filepath.Join(t.TempDir(), "not-a-plugin")
	if err := os.WriteFile(stub, []byte("#!/bin/sh\nexit 0\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	p, closeFn, err := Load(stub)
	if err == nil {
		if closeFn != nil {
			closeFn()
		}
		t.Fatal("expected a handshake error for a non-plugin binary")
	}
	if p != nil || closeFn != nil {
		t.Fatalf("failure must return nil plugin + nil close, got %v close=%t", p, closeFn != nil)
	}
}
