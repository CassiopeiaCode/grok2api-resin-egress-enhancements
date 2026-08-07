package relational

import (
	"context"
	"fmt"
	"path/filepath"
	"testing"

	"github.com/chenyme/grok2api/backend/internal/domain/egress"
)

func browserProxyCandidates(prefix string) []string {
	values := make([]string, egress.BrowserProxyAccountPoolSize)
	for slot := range values {
		values[slot] = fmt.Sprintf("%s_%02d", prefix, slot)
	}
	return values
}

func TestBrowserProxyAccountPoolPersistsAndKeepsTwentySlots(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "browser-proxy-pool.db")
	database, err := OpenSQLite(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	repository := NewEgressRepository(database)
	first, err := repository.EnsureBrowserProxyAccountPool(ctx, browserProxyCandidates("first"), egress.BrowserProxyAccountPoolSize)
	if err != nil {
		t.Fatal(err)
	}
	if len(first) != egress.BrowserProxyAccountPoolSize {
		t.Fatalf("pool size = %d", len(first))
	}
	database.Close()

	database, err = OpenSQLite(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	repository = NewEgressRepository(database)
	second, err := repository.EnsureBrowserProxyAccountPool(ctx, browserProxyCandidates("second"), egress.BrowserProxyAccountPoolSize)
	if err != nil {
		t.Fatal(err)
	}
	for slot := range first {
		if second[slot].Slot != slot || second[slot].ProxyAccount != first[slot].ProxyAccount {
			t.Fatalf("slot %d after restart = %#v, want %#v", slot, second[slot], first[slot])
		}
	}
}

func TestBrowserProxyAccountReplacementIsCompareAndSwap(t *testing.T) {
	ctx := context.Background()
	database, err := OpenSQLite(ctx, filepath.Join(t.TempDir(), "browser-proxy-cas.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	if err := database.InitializeSchema(ctx); err != nil {
		t.Fatal(err)
	}
	repository := NewEgressRepository(database)
	pool, err := repository.EnsureBrowserProxyAccountPool(ctx, browserProxyCandidates("initial"), egress.BrowserProxyAccountPoolSize)
	if err != nil {
		t.Fatal(err)
	}
	old := pool[7].ProxyAccount
	replaced, err := repository.ReplaceBrowserProxyAccount(ctx, 7, old, "replacement_current")
	if err != nil || !replaced {
		t.Fatalf("first replacement replaced=%v err=%v", replaced, err)
	}
	replaced, err = repository.ReplaceBrowserProxyAccount(ctx, 7, old, "replacement_late")
	if err != nil || replaced {
		t.Fatalf("late replacement replaced=%v err=%v", replaced, err)
	}
	current, err := repository.EnsureBrowserProxyAccountPool(ctx, browserProxyCandidates("unused"), egress.BrowserProxyAccountPoolSize)
	if err != nil {
		t.Fatal(err)
	}
	if current[7].ProxyAccount != "replacement_current" {
		t.Fatalf("slot 7 = %q", current[7].ProxyAccount)
	}
}
