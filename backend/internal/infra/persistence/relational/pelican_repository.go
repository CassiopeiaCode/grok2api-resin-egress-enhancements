package relational

import (
	"context"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/pelican"
)

func pelicanEntry(value pelicanEgressEntryModel) pelican.Entry {
	return pelican.Entry{ID: value.ID, ProxyUsername: value.ProxyUsername, Label: value.Label, Confidence: value.Confidence, ClassifierVer: value.ClassifierVer, Status: value.Status, LastCheckedAt: value.LastCheckedAt, NextCheckAt: value.NextCheckAt, CreatedAt: value.CreatedAt, UpdatedAt: value.UpdatedAt}
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
	row := pelicanEgressEntryModel{ID: value.ID, ProxyUsername: value.ProxyUsername, Label: value.Label, Confidence: value.Confidence, ClassifierVer: value.ClassifierVer, Status: value.Status, LastCheckedAt: value.LastCheckedAt, NextCheckAt: value.NextCheckAt, CreatedAt: value.CreatedAt, UpdatedAt: value.UpdatedAt}
	result := r.db.db.WithContext(ctx).Where("proxy_username = ?", value.ProxyUsername).Assign(map[string]any{"label": row.Label, "confidence": row.Confidence, "classifier_ver": row.ClassifierVer, "status": row.Status, "last_checked_at": row.LastCheckedAt, "next_check_at": row.NextCheckAt, "updated_at": now}).FirstOrCreate(&row)
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
