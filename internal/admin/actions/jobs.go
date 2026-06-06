package actions

import (
	"crypto/rand"
	"encoding/base64"
	"sync"
	"time"
)

// jobTTL is how long an unstreamed job survives. An admin who clicks Run then
// closes the tab leaves a job that expires rather than accumulating; the
// one-shot Pop also means a refresh of the streaming page does not re-run the
// action (the second GET sees a missing job).
const jobTTL = 60 * time.Second

// Job is a one-shot dispatch request created by POST /actions/start and
// consumed by GET /actions/stream.
type Job struct {
	ID         string
	ActionName string
	Payload    string
	Email      string
	SourceIP   string
	Category   string
	ArgKeys    []string
	createdAt  time.Time
}

// JobRegistry is a thread-safe one-shot registry of pending dispatch jobs.
type JobRegistry struct {
	mu   sync.Mutex
	jobs map[string]Job
	ttl  time.Duration
	now  func() time.Time // injectable for tests
}

// NewJobRegistry returns an empty registry with the default TTL.
func NewJobRegistry() *JobRegistry {
	return &JobRegistry{jobs: map[string]Job{}, ttl: jobTTL, now: time.Now}
}

// Create stores a one-shot job and returns it. The id is 128 bits of
// url-safe entropy.
func (r *JobRegistry) Create(j Job) Job {
	j.ID = newToken()
	j.createdAt = r.now()
	r.mu.Lock()
	defer r.mu.Unlock()
	r.sweepLocked()
	r.jobs[j.ID] = j
	return j
}

// Pop consumes a job by id: it returns the job and removes it. A second Pop of
// the same id returns ok=false (one-shot).
func (r *JobRegistry) Pop(id string) (Job, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.sweepLocked()
	j, ok := r.jobs[id]
	if ok {
		delete(r.jobs, id)
	}
	return j, ok
}

// Len reports the number of live (unexpired) jobs.
func (r *JobRegistry) Len() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.sweepLocked()
	return len(r.jobs)
}

func (r *JobRegistry) sweepLocked() {
	cutoff := r.now().Add(-r.ttl)
	for k, v := range r.jobs {
		if v.createdAt.Before(cutoff) {
			delete(r.jobs, k)
		}
	}
}

func newToken() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}
