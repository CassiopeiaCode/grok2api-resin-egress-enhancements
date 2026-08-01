package pelican

import (
	"context"
	"github.com/chenyme/grok2api/backend/internal/domain/pelican"
	"testing"
	"time"
)

type fakeRepo struct{ items []pelican.Entry }

func (f *fakeRepo) ListActivePelican(context.Context) ([]pelican.Entry, error) { return f.items, nil }
func (f *fakeRepo) ListDuePelican(context.Context, time.Time) ([]pelican.Entry, error) {
	return nil, nil
}
func (f *fakeRepo) UpsertPelican(_ context.Context, v pelican.Entry) (pelican.Entry, error) {
	f.items = append(f.items, v)
	return v, nil
}
func (f *fakeRepo) RemovePelican(context.Context, string) error { return nil }
func (f *fakeRepo) TouchPelican(context.Context, string, time.Time, time.Time, string, float64, string) error {
	return nil
}
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
