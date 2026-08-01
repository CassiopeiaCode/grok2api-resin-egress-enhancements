package pelican

import "time"

const Active = "active"

type Entry struct {
	ID            uint64
	ProxyUsername string
	Label         string
	Confidence    float64
	ClassifierVer string
	Status        string
	LastCheckedAt *time.Time
	NextCheckAt   time.Time
	CreatedAt     time.Time
	UpdatedAt     time.Time
}

type ProbeTask struct {
	ID            uint64
	Mode          string // explore or recheck
	EntryID       uint64
	ProxyUsername string
	AccountID     uint64
	EgressNodeID  uint64
	LeaseUntil    time.Time
}
