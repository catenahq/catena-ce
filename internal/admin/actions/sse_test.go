package actions

import "testing"

func TestFormatStdoutFramesPlainData(t *testing.T) {
	if got := FormatStdout("hello"); got != "data: hello\n\n" {
		t.Fatalf("got %q", got)
	}
}

func TestFormatStderrCarriesEventField(t *testing.T) {
	if got := FormatStderr("boom"); got != "event: stderr\ndata: boom\n\n" {
		t.Fatalf("got %q", got)
	}
}

func TestMultilineDataBecomesOneDataFieldPerLine(t *testing.T) {
	// A newline INSIDE a data payload must not terminate the SSE message
	// early -- each line gets its own data: field, one blank line ends it.
	got := sseMessage("", "line1\nline2")
	want := "data: line1\ndata: line2\n\n"
	if got != want {
		t.Fatalf("got %q want %q", got, want)
	}
}

func TestFormatStart(t *testing.T) {
	got := FormatStart("backup-now", "a@x")
	want := "event: start\ndata: action=backup-now email=a@x\n\n"
	if got != want {
		t.Fatalf("got %q", got)
	}
}

func TestFormatEnd(t *testing.T) {
	if got := FormatEnd(0, ""); got != "event: end\ndata: 0\n\n" {
		t.Fatalf("clean end: got %q", got)
	}
	got := FormatEnd(7, "ssh timeout")
	want := "event: end\ndata: 7\ndata: error: ssh timeout\n\n"
	if got != want {
		t.Fatalf("end with detail: got %q", got)
	}
}

// BuildCommand is covered by actions_test.go.
