package gateway

import (
	"io"
	"sync"
	"time"
)

const resinStreamSilenceTimeout = 90 * time.Second

// streamSilenceTracker watches complete upstream SSE data events. It starts
// at the time the upstream response is handed to the HTTP layer, so silence
// before the first event is covered as well. When the deadline expires it
// closes the upstream body to unblock the handler and records an explicit
// silence signal for Resin rotation.
//
// The tracker intentionally observes SSE events rather than TCP reads. A
// proxy can deliver arbitrary partial chunks without those chunks constituting
// an upstream event.
type streamSilenceTracker struct {
	mu         sync.Mutex
	body       io.ReadCloser
	timeout    time.Duration
	timer      *time.Timer
	generation uint64
	timedOut   bool
	closed     bool
}

func newStreamSilenceTracker(body io.ReadCloser, timeout time.Duration) *streamSilenceTracker {
	if timeout <= 0 {
		timeout = resinStreamSilenceTimeout
	}
	tracker := &streamSilenceTracker{body: body, timeout: timeout}
	tracker.mu.Lock()
	tracker.scheduleLocked()
	tracker.mu.Unlock()
	return tracker
}

func (t *streamSilenceTracker) scheduleLocked() {
	t.generation++
	generation := t.generation
	t.timer = time.AfterFunc(t.timeout, func() {
		var body io.ReadCloser
		t.mu.Lock()
		if t.closed || t.timedOut || generation != t.generation {
			t.mu.Unlock()
			return
		}
		t.timedOut = true
		body = t.body
		t.mu.Unlock()
		if body != nil {
			_ = body.Close()
		}
	})
}

// MarkEvent resets the silence deadline after one upstream SSE data event.
func (t *streamSilenceTracker) MarkEvent() {
	if t == nil {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed || t.timedOut {
		return
	}
	if t.timer != nil {
		_ = t.timer.Stop()
	}
	t.scheduleLocked()
}

func (t *streamSilenceTracker) TimedOut() bool {
	if t == nil {
		return false
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.timedOut
}

// Close stops future deadlines when the response has completed normally or
// the gateway finalizer is running for another reason.
func (t *streamSilenceTracker) Close() {
	if t == nil {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed {
		return
	}
	t.closed = true
	t.generation++
	if t.timer != nil {
		_ = t.timer.Stop()
	}
}
