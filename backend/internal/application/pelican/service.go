package pelican

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/pelican"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

type Service struct {
	repo     repository.PelicanRepository
	target   int
	interval time.Duration
}

func NewService(repo repository.PelicanRepository) *Service {
	return &Service{repo: repo, target: 3, interval: 10 * time.Minute}
}

func (s *Service) List(ctx context.Context) ([]pelican.Entry, error) {
	return s.repo.ListActivePelican(ctx)
}
func (s *Service) Bad(ctx context.Context) ([]string, error) {
	return s.repo.ListBadPelican(ctx, time.Now().UTC())
}
func (s *Service) Due(ctx context.Context) ([]pelican.Entry, error) {
	return s.repo.ListDuePelican(ctx, time.Now().UTC())
}
func (s *Service) Target() int { return s.target }

func (s *Service) Select(ctx context.Context, accountID uint64) (string, error) {
	items, err := s.repo.ListActivePelican(ctx)
	if err != nil {
		return "", err
	}
	if len(items) == 0 {
		return "", nil
	}
	type score struct {
		name  string
		value string
	}
	scores := make([]score, 0, len(items))
	for _, item := range items {
		h := sha256.Sum256([]byte(fmt.Sprintf("%d:%s", accountID, item.ProxyUsername)))
		scores = append(scores, score{item.ProxyUsername, hex.EncodeToString(h[:])})
	}
	sort.Slice(scores, func(i, j int) bool { return scores[i].value > scores[j].value })
	return scores[0].name, nil
}

func (s *Service) Admit(ctx context.Context, username, exitIP string, confidence float64, version string) error {
	if confidence < .60 {
		return fmt.Errorf("pelican candidate confidence below admission threshold")
	}
	if strings.TrimSpace(exitIP) == "" {
		return fmt.Errorf("pelican candidate exit_ip is required")
	}
	now := time.Now().UTC()
	if err := s.repo.ClearBadPelican(ctx, username, exitIP); err != nil {
		return err
	}
	_, err := s.repo.UpsertPelican(ctx, pelican.Entry{ProxyUsername: username, ExitIP: exitIP, Label: "good", Confidence: confidence, ClassifierVer: version, Status: pelican.Active, LastCheckedAt: &now, NextCheckAt: now.Add(s.interval)})
	return err
}
func (s *Service) Keep(ctx context.Context, username string, confidence float64, version string) error {
	now := time.Now().UTC()
	return s.repo.TouchPelican(ctx, username, now, now.Add(s.interval), "good", confidence, version)
}
func (s *Service) Evict(ctx context.Context, username string) error {
	return s.repo.RemovePelican(ctx, username)
}
func (s *Service) MarkBad(ctx context.Context, username, exitIP, reason string) error {
	return s.repo.MarkBadPelican(ctx, username, exitIP, reason, time.Now().UTC().Add(24*time.Hour))
}
func (s *Service) PoolSize(ctx context.Context) (int, error) { v, e := s.List(ctx); return len(v), e }
