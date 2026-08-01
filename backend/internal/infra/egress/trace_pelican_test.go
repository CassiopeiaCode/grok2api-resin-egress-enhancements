package egress

import (
	"context"
	accountdomain "github.com/chenyme/grok2api/backend/internal/domain/account"
	"testing"
)

func TestWithCredentialPreservesForcedPelicanIdentity(t *testing.T) {
	ctx := WithAccountIdentity(context.Background(), "Default.pelican-fixed")
	got := WithCredential(ctx, accountdomain.Credential{Provider: accountdomain.ProviderBuild, ID: 42, ResinAccountSuffix: "old"})
	if value := AccountFromContext(got); value != "Default.pelican-fixed" {
		t.Fatalf("identity=%q", value)
	}
}
