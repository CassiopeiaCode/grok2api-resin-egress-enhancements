package repository

import (
	"context"
	"github.com/chenyme/grok2api/backend/internal/domain/pelican"
	"time"
)

type PelicanRepository interface {
	ListActivePelican(ctx context.Context) ([]pelican.Entry, error)
	ListDuePelican(ctx context.Context, now time.Time) ([]pelican.Entry, error)
	UpsertPelican(ctx context.Context, value pelican.Entry) (pelican.Entry, error)
	RemovePelican(ctx context.Context, username string) error
	TouchPelican(ctx context.Context, username string, checked, next time.Time, label string, confidence float64, version string) error
	ListBadPelican(ctx context.Context, now time.Time) ([]string, error)
	MarkBadPelican(ctx context.Context, username, exitIP, reason string, expires time.Time) error
	ClearBadPelican(ctx context.Context, username, exitIP string) error
}
