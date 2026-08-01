package adminquality

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/chenyme/grok2api/backend/internal/application/gateway"
	"github.com/chenyme/grok2api/backend/internal/domain/account"
	clientkeydomain "github.com/chenyme/grok2api/backend/internal/domain/clientkey"
	"github.com/chenyme/grok2api/backend/internal/transport/http/inference"
	"github.com/gin-gonic/gin"
)

// Handler serves tightly scoped administrator quality experiments. The
// request body is deliberately passed through unchanged; no test prompt is
// injected by this endpoint.
type Handler struct {
	gateway   *gateway.Service
	inference *inference.Handler
}

type requestEnvelope struct {
	Provider        string          `json:"provider"`
	AccountID       uint64          `json:"account_id"`
	EgressNodeID    uint64          `json:"egress_node_id"`
	ProxyUsername   string          `json:"proxy_username"`
	Model           string          `json:"model"`
	Stream          *bool           `json:"stream"`
	Request         json.RawMessage `json:"request"`
	Body            json.RawMessage `json:"body"`
}

type requestMeta struct {
	Model  string `json:"model"`
	Stream bool   `json:"stream"`
}

func NewHandler(g *gateway.Service, i *inference.Handler) *Handler {
	return &Handler{gateway: g, inference: i}
}

func (h *Handler) Register(router *gin.RouterGroup) {
	router.POST("/quality-tests/requests", h.request)
}

func (h *Handler) request(c *gin.Context) {
	var envelope requestEnvelope
	if err := c.ShouldBindJSON(&envelope); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"code": "invalid_request", "message": "请求体必须是 JSON"}})
		return
	}
	provider := account.Provider(strings.TrimSpace(envelope.Provider))
	if provider != account.ProviderBuild && provider != account.ProviderWeb && provider != account.ProviderConsole {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"code": "invalid_provider", "message": "provider 必须是 grok_build、grok_web 或 grok_console"}})
		return
	}
	body := envelope.Request
	if len(body) == 0 {
		body = envelope.Body
	}
	if len(body) == 0 || string(body) == "null" {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"code": "missing_request", "message": "必须提供 request 或 body"}})
		return
	}
	var meta requestMeta
	if err := json.Unmarshal(body, &meta); err != nil || strings.TrimSpace(meta.Model) == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"code": "invalid_request", "message": "request 必须包含 model"}})
		return
	}
	if envelope.Model != "" {
		meta.Model = strings.TrimSpace(envelope.Model)
	}
	if envelope.Stream != nil {
		meta.Stream = *envelope.Stream
	}
	if envelope.AccountID == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"code": "missing_account_id", "message": "必须指定 account_id"}})
		return
	}
	proxyUsername := strings.TrimSpace(envelope.ProxyUsername)
	if len(proxyUsername) > 128 || strings.ContainsAny(proxyUsername, "\r\n@") {
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{"code": "invalid_proxy_username", "message": "proxy_username 无效或过长"}})
		return
	}
	publicModel := meta.Model
	if !strings.HasPrefix(strings.ToLower(publicModel), strings.ToLower(provider.ModelNamespace()+"/")) {
		publicModel = provider.ModelNamespace() + "/" + publicModel
	}
	requestID := c.GetHeader("X-Request-ID")
	if requestID == "" {
		requestID = "admin-quality-" + strconv.FormatInt(time.Now().UnixNano(), 10)
	}
	// An unrestricted synthetic key is used only after AdminAuth. Billing is
	// explicitly disabled by AdminQualityTest, while normal audit/finalization
	// remains active for later inspection of token timing.
	key := clientkeydomain.Key{ID: 0, Name: "admin-quality-test", Enabled: true, ProviderScope: clientkeydomain.ProviderScopeAll, TierScope: clientkeydomain.TierScopeAll}
	result, err := h.gateway.CreateResponse(c.Request.Context(), gateway.Input{
		RequestID: requestID, ClientKey: key, PublicModel: publicModel, Body: body,
		Streaming: meta.Stream, AdminQualityTest: true, ForcedAccountID: envelope.AccountID,
		ForcedEgressNodeID: envelope.EgressNodeID, ForcedProxyUsername: proxyUsername,
	})
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": gin.H{"code": "quality_test_failed", "message": err.Error()}})
		return
	}
	h.inference.WriteAdminResult(c, result, meta.Stream)
}
