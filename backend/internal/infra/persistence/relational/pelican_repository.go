package relational

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/pelican"
	"gorm.io/gorm"
)

func pelicanEntry(value pelicanEgressEntryModel) pelican.Entry {
	return pelican.Entry{ID: value.ID, ProxyUsername: value.ProxyUsername, ExitIP: value.ExitIP, Label: value.Label, Confidence: value.Confidence, ClassifierVer: value.ClassifierVer, Status: value.Status, LastCheckedAt: value.LastCheckedAt, NextCheckAt: value.NextCheckAt, CreatedAt: value.CreatedAt, UpdatedAt: value.UpdatedAt}
}

func (r *AccountRepository) ListActivePelican(ctx context.Context) ([]pelican.Entry, error) {
	var rows []pelicanEgressEntryModel
	if err := r.db.db.WithContext(ctx).Where("status = ? AND label = ?", pelican.Active, "good").Order("id ASC").Find(&rows).Error; err != nil {
		return nil, err
	}
	out := make([]pelican.Entry, 0, len(rows))
	for _, row := range rows {
		out = append(out, pelicanEntry(row))
	}
	return out, nil
}

func (r *AccountRepository) ListDuePelican(ctx context.Context, now time.Time) ([]pelican.Entry, error) {
	var rows []pelicanEgressEntryModel
	if err := r.db.db.WithContext(ctx).Where("status = ? AND next_check_at <= ?", pelican.Active, now).Order("next_check_at ASC, id ASC").Find(&rows).Error; err != nil {
		return nil, err
	}
	out := make([]pelican.Entry, 0, len(rows))
	for _, row := range rows {
		out = append(out, pelicanEntry(row))
	}
	return out, nil
}

func (r *AccountRepository) UpsertPelican(ctx context.Context, value pelican.Entry) (pelican.Entry, error) {
	now := time.Now().UTC()
	if value.CreatedAt.IsZero() {
		value.CreatedAt = now
	}
	value.UpdatedAt = now
	if value.Status == "" {
		value.Status = pelican.Active
	}
	row := pelicanEgressEntryModel{ID: value.ID, ProxyUsername: value.ProxyUsername, ExitIP: value.ExitIP, Label: value.Label, Confidence: value.Confidence, ClassifierVer: value.ClassifierVer, Status: value.Status, LastCheckedAt: value.LastCheckedAt, NextCheckAt: value.NextCheckAt, CreatedAt: value.CreatedAt, UpdatedAt: now}
	result := r.db.db.WithContext(ctx).Where("proxy_username = ?", value.ProxyUsername).Assign(map[string]any{"exit_ip": row.ExitIP, "label": row.Label, "confidence": row.Confidence, "classifier_ver": row.ClassifierVer, "status": row.Status, "last_checked_at": row.LastCheckedAt, "next_check_at": row.NextCheckAt, "updated_at": now}).FirstOrCreate(&row)
	if result.Error != nil {
		return pelican.Entry{}, result.Error
	}
	return pelicanEntry(row), nil
}

func (r *AccountRepository) RemovePelican(ctx context.Context, username string) error {
	return r.db.db.WithContext(ctx).Model(&pelicanEgressEntryModel{}).Where("proxy_username = ?", username).Updates(map[string]any{"status": "removed", "updated_at": time.Now().UTC()}).Error
}

func (r *AccountRepository) TouchPelican(ctx context.Context, username string, checked, next time.Time, label string, confidence float64, version string) error {
	return r.db.db.WithContext(ctx).Model(&pelicanEgressEntryModel{}).Where("proxy_username = ?", username).Updates(map[string]any{"last_checked_at": checked, "next_check_at": next, "label": label, "confidence": confidence, "classifier_ver": version, "status": pelican.Active, "updated_at": time.Now().UTC()}).Error
}

func (r *AccountRepository) ListBadPelican(ctx context.Context, now time.Time) ([]string, error) {
	var rows []pelicanBadEgressModel
	query := r.db.db.WithContext(ctx)
	// Expired rows are not part of the active blacklist.  Delete them during
	// reads so the table does not grow forever even when no new probes arrive.
	if err := query.Where("expires_at <= ?", now).Delete(&pelicanBadEgressModel{}).Error; err != nil {
		return nil, err
	}
	if err := query.Where("expires_at > ? AND exit_ip <> ''", now).Order("id ASC").Find(&rows).Error; err != nil {
		return nil, err
	}
	out := make([]string, 0, len(rows))
	seen := make(map[string]struct{}, len(rows))
	for _, row := range rows {
		if row.ExitIP != "" {
			if _, ok := seen[row.ExitIP]; ok {
				continue
			}
			seen[row.ExitIP] = struct{}{}
			out = append(out, row.ExitIP)
		}
	}
	return out, nil
}

func (r *AccountRepository) MarkBadPelican(ctx context.Context, username, exitIP, reason string, expires time.Time) error {
	username = strings.TrimSpace(username)
	exitIP = strings.TrimSpace(exitIP)
	reason = strings.TrimSpace(reason)
	// The blacklist is deliberately keyed by the public exit IP.  If trace
	// could not determine an IP, do not create a username-only blacklist row;
	// such a row could not protect another Resin username sharing that exit.
	if exitIP == "" {
		return nil
	}
	now := time.Now().UTC()
	var row pelicanBadEgressModel
	query := r.db.db.WithContext(ctx).Where("exit_ip = ?", exitIP).First(&row)
	if errors.Is(query.Error, gorm.ErrRecordNotFound) {
		row = pelicanBadEgressModel{ProxyUsername: username, ExitIP: exitIP, Reason: reason, ExpiresAt: expires, CreatedAt: now, UpdatedAt: now}
		return r.db.db.WithContext(ctx).Create(&row).Error
	}
	if query.Error != nil {
		return query.Error
	}
	return r.db.db.WithContext(ctx).Model(&row).Updates(map[string]any{"proxy_username": username, "reason": reason, "expires_at": expires, "updated_at": now}).Error
}

func (r *AccountRepository) ClearBadPelican(ctx context.Context, username, exitIP string) error {
	username = strings.TrimSpace(username)
	exitIP = strings.TrimSpace(exitIP)
	if username == "" && exitIP == "" {
		return nil
	}
	q := r.db.db.WithContext(ctx).Model(&pelicanBadEgressModel{})
	if username != "" && exitIP != "" {
		return q.Where("proxy_username = ? OR exit_ip = ?", username, exitIP).Delete(&pelicanBadEgressModel{}).Error
	}
	if username != "" {
		return q.Where("proxy_username = ?", username).Delete(&pelicanBadEgressModel{}).Error
	}
	return q.Where("exit_ip = ?", exitIP).Delete(&pelicanBadEgressModel{}).Error
}
