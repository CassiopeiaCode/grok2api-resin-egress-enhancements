package relational

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/chenyme/grok2api/backend/internal/domain/egress"
	"gorm.io/gorm/clause"
)

func browserProxyAccount(value browserProxyAccountModel) egress.BrowserProxyAccount {
	return egress.BrowserProxyAccount{
		Slot: value.Slot, ProxyAccount: value.ProxyAccount,
		CreatedAt: value.CreatedAt, UpdatedAt: value.UpdatedAt,
	}
}

// EnsureBrowserProxyAccountPool fills missing stable slots without changing
// existing identities. Unique slot/account constraints make concurrent
// initialization by multiple replicas idempotent.
func (r *EgressRepository) EnsureBrowserProxyAccountPool(ctx context.Context, candidates []string, size int) ([]egress.BrowserProxyAccount, error) {
	if size != egress.BrowserProxyAccountPoolSize || len(candidates) < size {
		return nil, fmt.Errorf("Web/Console 代理账号池参数无效")
	}
	var existing []browserProxyAccountModel
	if err := r.db.db.WithContext(ctx).Order("slot ASC").Find(&existing).Error; err != nil {
		return nil, err
	}
	if len(existing) == size {
		values := make([]egress.BrowserProxyAccount, 0, len(existing))
		for _, row := range existing {
			values = append(values, browserProxyAccount(row))
		}
		return values, nil
	}
	present := make(map[int]struct{}, len(existing))
	for _, row := range existing {
		present[row.Slot] = struct{}{}
	}
	now := time.Now().UTC()
	for slot := 0; slot < size; slot++ {
		if _, ok := present[slot]; ok {
			continue
		}
		account := strings.TrimSpace(candidates[slot])
		if account == "" || len(account) > 128 {
			return nil, fmt.Errorf("Web/Console 代理账号无效")
		}
		row := browserProxyAccountModel{Slot: slot, ProxyAccount: account, CreatedAt: now, UpdatedAt: now}
		if err := r.db.db.WithContext(ctx).Clauses(clause.OnConflict{Columns: []clause.Column{{Name: "slot"}}, DoNothing: true}).Create(&row).Error; err != nil {
			return nil, err
		}
	}
	var rows []browserProxyAccountModel
	if err := r.db.db.WithContext(ctx).Order("slot ASC").Find(&rows).Error; err != nil {
		return nil, err
	}
	if len(rows) != size {
		return nil, fmt.Errorf("Web/Console 代理账号池不完整: %d/%d", len(rows), size)
	}
	values := make([]egress.BrowserProxyAccount, 0, len(rows))
	for _, row := range rows {
		values = append(values, browserProxyAccount(row))
	}
	sort.Slice(values, func(i, j int) bool { return values[i].Slot < values[j].Slot })
	return values, nil
}

// ReplaceBrowserProxyAccount uses the observed identity as a compare-and-swap
// guard. A late timeout from another replica cannot discard a newer member.
func (r *EgressRepository) ReplaceBrowserProxyAccount(ctx context.Context, slot int, expected, replacement string) (bool, error) {
	expected, replacement = strings.TrimSpace(expected), strings.TrimSpace(replacement)
	if slot < 0 || slot >= egress.BrowserProxyAccountPoolSize || expected == "" || replacement == "" || len(replacement) > 128 {
		return false, fmt.Errorf("Web/Console 代理账号替换参数无效")
	}
	result := r.db.db.WithContext(ctx).Model(&browserProxyAccountModel{}).
		Where("slot = ? AND proxy_account = ?", slot, expected).
		Updates(map[string]any{"proxy_account": replacement, "updated_at": time.Now().UTC()})
	if result.Error != nil {
		return false, result.Error
	}
	return result.RowsAffected == 1, nil
}

var _ interface {
	EnsureBrowserProxyAccountPool(context.Context, []string, int) ([]egress.BrowserProxyAccount, error)
	ReplaceBrowserProxyAccount(context.Context, int, string, string) (bool, error)
} = (*EgressRepository)(nil)
