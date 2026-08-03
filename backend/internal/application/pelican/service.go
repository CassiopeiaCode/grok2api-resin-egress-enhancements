package pelican

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/pelican"
	"github.com/chenyme/grok2api/backend/internal/repository"
)

type Service struct {
	repo                 repository.PelicanRepository
	target               int
	interval             time.Duration
	qualityMu            sync.Mutex
	fastStreaks          map[string]int
	headerTimeoutStreaks map[string]int
}

func NewService(repo repository.PelicanRepository) *Service {
	return &Service{
		repo: repo, target: 5, interval: 10 * time.Minute,
		fastStreaks: make(map[string]int), headerTimeoutStreaks: make(map[string]int),
	}
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
	if confidence < .55 {
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
	s.qualityMu.Lock()
	delete(s.fastStreaks, strings.TrimSpace(username))
	delete(s.headerTimeoutStreaks, strings.TrimSpace(username))
	s.qualityMu.Unlock()
	return s.repo.RemovePelican(ctx, username)
}

func (s *Service) evictObserved(ctx context.Context, items []pelican.Entry, username, reason string) (bool, error) {
	exitIP := ""
	active := false
	for _, item := range items {
		if item.ProxyUsername == username {
			exitIP = strings.TrimSpace(item.ExitIP)
			active = true
			break
		}
	}
	if !active {
		return false, nil
	}
	if err := s.repo.RemovePelican(ctx, username); err != nil {
		return false, err
	}
	if exitIP != "" {
		if err := s.repo.MarkBadPelican(ctx, username, exitIP, reason, time.Now().UTC().Add(24*time.Hour)); err != nil {
			return true, err
		}
	}
	return true, nil
}

// ObserveStreamSpeed consumes only successful measured production streams.
// A normal-speed result resets the lease's streak; the second consecutive
// result above the threshold removes the exact username used by the requests.
// Its traced exit IP is quarantined as well, so a newly generated username
// cannot immediately re-enter through the same known-bad public exit.
func (s *Service) ObserveStreamSpeed(ctx context.Context, username string, speed, threshold float64) (bool, int, error) {
	username = strings.TrimSpace(username)
	if username == "" {
		return false, 0, nil
	}
	s.qualityMu.Lock()
	if speed <= threshold {
		delete(s.fastStreaks, username)
		s.qualityMu.Unlock()
		return false, 0, nil
	}
	streak := s.fastStreaks[username] + 1
	s.fastStreaks[username] = streak
	if streak < 2 {
		s.qualityMu.Unlock()
		return false, streak, nil
	}
	delete(s.fastStreaks, username)
	s.qualityMu.Unlock()

	items, err := s.repo.ListActivePelican(ctx)
	if err != nil {
		return false, streak, err
	}
	evicted, err := s.evictObserved(ctx, items, username, "consecutive_fast_streams")
	return evicted, streak, err
}

// ResetResponseHeaderTimeout interrupts the consecutive-timeout sequence as
// soon as a production Build stream receives upstream response headers.
func (s *Service) ResetResponseHeaderTimeout(username string) {
	username = strings.TrimSpace(username)
	if username == "" {
		return
	}
	s.qualityMu.Lock()
	delete(s.headerTimeoutStreaks, username)
	s.qualityMu.Unlock()
}

// ObserveResponseHeaderTimeout evicts only while the pool has comfortable
// spare capacity: strictly more than two thirds of the configured maximum.
// At target 5 this means 4-5 active leases. Two consecutive timeouts are
// required for the same username, and callers restrict samples to Build
// streaming production requests.
func (s *Service) ObserveResponseHeaderTimeout(ctx context.Context, username string) (bool, int, error) {
	username = strings.TrimSpace(username)
	if username == "" {
		return false, 0, nil
	}
	items, err := s.repo.ListActivePelican(ctx)
	if err != nil {
		return false, 0, err
	}
	if len(items) <= (s.target*2)/3 {
		s.ResetResponseHeaderTimeout(username)
		return false, 0, nil
	}
	s.qualityMu.Lock()
	streak := s.headerTimeoutStreaks[username] + 1
	s.headerTimeoutStreaks[username] = streak
	if streak < 2 {
		s.qualityMu.Unlock()
		return false, streak, nil
	}
	delete(s.headerTimeoutStreaks, username)
	s.qualityMu.Unlock()
	evicted, err := s.evictObserved(ctx, items, username, "consecutive_stream_header_timeouts")
	return evicted, streak, err
}
func (s *Service) MarkBad(ctx context.Context, username, exitIP, reason string) error {
	return s.repo.MarkBadPelican(ctx, username, exitIP, reason, time.Now().UTC().Add(24*time.Hour))
}
func (s *Service) PoolSize(ctx context.Context) (int, error) { v, e := s.List(ctx); return len(v), e }
