package adminquality

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/chenyme/grok2api/backend/internal/application/gateway"
	clientkeyapp "github.com/chenyme/grok2api/backend/internal/application/clientkey"
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
	clientKeys *clientkeyapp.Service
}

type requestEnvelope struct {
	Provider        string          `json:"provider"`
	AccountID       uint64          `json:"account_id"`
	EgressNodeID    uint64          `json:"egress_node_id"`
	ProxyUsername   string          `json:"proxy_username"`
	Operation       string          `json:"operation"`
	Model           string          `json:"model"`
	Stream          *bool           `json:"stream"`
	Request         json.RawMessage `json:"request"`
	Body            json.RawMessage `json:"body"`
}

type requestMeta struct {
	Model  string `json:"model"`
	Stream bool   `json:"stream"`
}

func NewHandler(g *gateway.Service, i *inference.Handler, keys *clientkeyapp.Service) *Handler {
	return &Handler{gateway: g, inference: i, clientKeys: keys}
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
	key, err := h.ensureAuditKey(c)
	if err != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": gin.H{"code": "quality_test_audit_unavailable", "message": "无法准备质量测试审计身份"}})
		return
	}
	input := gateway.Input{
		RequestID: requestID, ClientKey: key, PublicModel: publicModel, Body: body,
		Streaming: meta.Stream, AdminQualityTest: true, ForcedAccountID: envelope.AccountID,
		ForcedEgressNodeID: envelope.EgressNodeID, ForcedProxyUsername: proxyUsername,
	}
	var result *gateway.Result
	chatProtocol := true
	if strings.EqualFold(strings.TrimSpace(envelope.Operation), "responses") {
		chatProtocol = false
		result, err = h.gateway.CreateResponse(c.Request.Context(), input)
	} else {
		// Chat is the default because it matches the production Build request
		// shape used by the token-speed samples.
		result, err = h.gateway.CreateChatCompletion(c.Request.Context(), input)
	}
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": gin.H{"code": "quality_test_failed", "message": err.Error()}})
		return
	}
	if chatProtocol {
		h.inference.WriteAdminChatResult(c, result, meta.Stream)
	} else {
		h.inference.WriteAdminResult(c, result, meta.Stream)
	}
}

// ensureAuditKey returns a real persisted client-key row so request_audits'
// positive foreign-key/check constraints remain intact. It is never exposed
// to downstream clients and AdminQualityTest disables billing reservation.
func (h *Handler) ensureAuditKey(c *gin.Context) (clientkeydomain.Key, error) {
	if h.clientKeys == nil {
		return clientkeydomain.Key{}, fmt.Errorf("client key service unavailable")
	}
	values, _, err := h.clientKeys.List(c.Request.Context(), 1, 20, "__admin_quality_test__", clientkeyapp.ListFilter{})
	if err != nil {
		return clientkeydomain.Key{}, err
	}
	for _, value := range values {
		if value.Name == "__admin_quality_test__" {
			value.Enabled = true
			value.ProviderScope = clientkeydomain.ProviderScopeAll
			value.TierScope = clientkeydomain.TierScopeAll
			return value, nil
		}
	}
	created, err := h.clientKeys.Create(c.Request.Context(), clientkeyapp.CreateInput{
		Name: "__admin_quality_test__", Enabled: true, RPMUnlimited: true,
		ConcurrencyUnlimited: true, BillingLimitUSDTicks: 0,
		ProviderScope: clientkeydomain.ProviderScopeAll, TierScope: clientkeydomain.TierScopeAll,
	})
	if err != nil {
		return clientkeydomain.Key{}, err
	}
	return created.Key, nil
}
