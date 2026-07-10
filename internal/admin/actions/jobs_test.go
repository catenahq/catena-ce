package actions

import (
	"testing"
	"time"
)

func TestCreateAssignsUniqueIDs(t *testing.T) {
	r := NewJobRegistry()
	a := r.Create(Job{ActionName: "backup-now"})
	b := r.Create(Job{ActionName: "backup-now"})
	if a.ID == "" || b.ID == "" || a.ID == b.ID {
		t.Fatalf("expected two distinct non-empty ids, got %q %q", a.ID, b.ID)
	}
	if r.Len() != 2 {
		t.Fatalf("Len: got %d", r.Len())
	}
}

func TestPopIsOneShot(t *testing.T) {
	r := NewJobRegistry()
	j := r.Create(Job{ActionName: "backup-now", Email: "a@x", Payload: "p"})

	got, ok := r.Pop(j.ID)
	if !ok || got.ActionName != "backup-now" || got.Email != "a@x" || got.Payload != "p" {
		t.Fatalf("first Pop: ok=%v job=%+v", ok, got)
	}
	// A refresh of the streaming page must NOT re-run the action.
	if _, ok := r.Pop(j.ID); ok {
		t.Fatal("second Pop of the same id must return ok=false")
	}
	if r.Len() != 0 {
		t.Fatalf("Len after pop: got %d", r.Len())
	}
}

func TestPopUnknownID(t *testing.T) {
	r := NewJobRegistry()
	if _, ok := r.Pop("nope"); ok {
		t.Fatal("unknown id must return ok=false")
	}
}

func TestExpiredJobsAreSwept(t *testing.T) {
	r := NewJobRegistry()
	now := time.Now()
	r.now = func() time.Time { return now }

	j := r.Create(Job{ActionName: "backup-now"})

	// Just inside the TTL: still poppable.
	r.now = func() time.Time { return now.Add(jobTTL - time.Second) }
	if r.Len() != 1 {
		t.Fatal("job expired too early")
	}

	// Past the TTL: swept on the next registry touch (the click-run-then-
	// close-the-tab case must not accumulate).
	r.now = func() time.Time { return now.Add(jobTTL + time.Second) }
	if _, ok := r.Pop(j.ID); ok {
		t.Fatal("expired job must not be poppable")
	}
	if r.Len() != 0 {
		t.Fatalf("Len after expiry: got %d", r.Len())
	}
}
