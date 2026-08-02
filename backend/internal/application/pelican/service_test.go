package pelican

import (
	"context"
	"fmt"
	"github.com/chenyme/grok2api/backend/internal/domain/pelican"
	"testing"
	"time"
)

type fakeRepo struct {
	items      []pelican.Entry
	badReasons []string
}

func (f *fakeRepo) ListActivePelican(context.Context) ([]pelican.Entry, error) { return f.items, nil }
func (f *fakeRepo) ListDuePelican(context.Context, time.Time) ([]pelican.Entry, error) {
	return nil, nil
}
func (f *fakeRepo) UpsertPelican(_ context.Context, v pelican.Entry) (pelican.Entry, error) {
	f.items = append(f.items, v)
	return v, nil
}
func (f *fakeRepo) RemovePelican(_ context.Context, username string) error {
	kept := f.items[:0]
	for _, item := range f.items {
		if item.ProxyUsername != username {
			kept = append(kept, item)
		}
	}
	f.items = kept
	return nil
}
func (f *fakeRepo) TouchPelican(context.Context, string, time.Time, time.Time, string, float64, string) error {
	return nil
}
func (f *fakeRepo) ListBadPelican(context.Context, time.Time) ([]string, error) { return nil, nil }
func (f *fakeRepo) MarkBadPelican(_ context.Context, _, _, reason string, _ time.Time) error {
	f.badReasons = append(f.badReasons, reason)
	return nil
}
func (f *fakeRepo) ClearBadPelican(context.Context, string, string) error { return nil }
func TestSelectIsStable(t *testing.T) {
	f := &fakeRepo{items: []pelican.Entry{{ProxyUsername: "Default.pelican-a", Label: "good"}, {ProxyUsername: "Default.pelican-b", Label: "good"}, {ProxyUsername: "Default.pelican-c", Label: "good"}}}
	s := NewService(f)
	a, e := s.Select(context.Background(), 42)
	if e != nil {
		t.Fatal(e)
	}
	b, e := s.Select(context.Background(), 42)
	if e != nil || a != b {
		t.Fatalf("%q %q %v", a, b, e)
	}
}

func TestTargetAndConsecutiveFastStreamEviction(t *testing.T) {
	f := &fakeRepo{items: []pelican.Entry{{ProxyUsername: "Default.pelican-a", ExitIP: "203.0.113.1", Label: "good"}}}
	s := NewService(f)
	if s.Target() != 15 {
		t.Fatalf("target=%d", s.Target())
	}
	if evicted, streak, err := s.ObserveStreamSpeed(context.Background(), "Default.pelican-a", 250, 200); err != nil || evicted || streak != 1 {
		t.Fatalf("first fast sample: evicted=%v streak=%d err=%v", evicted, streak, err)
	}
	if evicted, streak, err := s.ObserveStreamSpeed(context.Background(), "Default.pelican-a", 180, 200); err != nil || evicted || streak != 0 {
		t.Fatalf("normal sample reset: evicted=%v streak=%d err=%v", evicted, streak, err)
	}
	_, _, _ = s.ObserveStreamSpeed(context.Background(), "Default.pelican-a", 250, 200)
	if evicted, streak, err := s.ObserveStreamSpeed(context.Background(), "Default.pelican-a", 201, 200); err != nil || !evicted || streak != 2 {
		t.Fatalf("second consecutive fast sample: evicted=%v streak=%d err=%v", evicted, streak, err)
	}
	if len(f.items) != 0 || len(f.badReasons) != 1 || f.badReasons[0] != "consecutive_fast_streams" {
		t.Fatalf("items=%#v bad=%#v", f.items, f.badReasons)
	}
}

func TestHeaderTimeoutEvictionRequiresPoolAboveTwoThirds(t *testing.T) {
	entries := func(count int) []pelican.Entry {
		out := make([]pelican.Entry, 0, count)
		for i := 0; i < count; i++ {
			out = append(out, pelican.Entry{ProxyUsername: fmt.Sprintf("Default.pelican-%d", i), ExitIP: fmt.Sprintf("203.0.113.%d", i+1), Label: "good"})
		}
		return out
	}
	f := &fakeRepo{items: entries(10)}
	s := NewService(f)
	if evicted, streak, err := s.ObserveResponseHeaderTimeout(context.Background(), "Default.pelican-0"); err != nil || evicted || streak != 0 {
		t.Fatalf("ten-entry pool: evicted=%v streak=%d err=%v", evicted, streak, err)
	}
	f.items = entries(11)
	if evicted, streak, err := s.ObserveResponseHeaderTimeout(context.Background(), "Default.pelican-0"); err != nil || evicted || streak != 1 {
		t.Fatalf("first timeout: evicted=%v streak=%d err=%v", evicted, streak, err)
	}
	s.ResetResponseHeaderTimeout("Default.pelican-0")
	_, _, _ = s.ObserveResponseHeaderTimeout(context.Background(), "Default.pelican-0")
	if evicted, streak, err := s.ObserveResponseHeaderTimeout(context.Background(), "Default.pelican-0"); err != nil || !evicted || streak != 2 {
		t.Fatalf("second consecutive timeout: evicted=%v streak=%d err=%v", evicted, streak, err)
	}
	if len(f.items) != 10 || len(f.badReasons) != 1 || f.badReasons[0] != "consecutive_stream_header_timeouts" {
		t.Fatalf("items=%d bad=%#v", len(f.items), f.badReasons)
	}
}
