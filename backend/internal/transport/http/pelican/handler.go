package pelican

import (
	"github.com/chenyme/grok2api/backend/internal/application/pelican"
	"github.com/gin-gonic/gin"
	"net/http"
	"strings"
)

type Handler struct{ service *pelican.Service }

func NewHandler(service *pelican.Service) *Handler { return &Handler{service: service} }
func (h *Handler) Register(r *gin.RouterGroup) {
	r.GET("/pelican-egress-pool", h.list)
	r.GET("/pelican-egress-pool/bad", h.bad)
	r.POST("/pelican-egress-pool/results", h.result)
}
func (h *Handler) list(c *gin.Context) {
	v, err := h.service.List(c.Request.Context())
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": v, "target": h.service.Target()})
}

func (h *Handler) bad(c *gin.Context) {
	v, err := h.service.Bad(c.Request.Context())
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"items": v})
}

type resultRequest struct {
	ProxyUsername     string  `json:"proxy_username"`
	ExitIP            string  `json:"exit_ip"`
	Label             string  `json:"label"`
	Confidence        float64 `json:"confidence"`
	ClassifierVersion string  `json:"classifier_version"`
}

func (h *Handler) result(c *gin.Context) {
	var in resultRequest
	if err := c.ShouldBindJSON(&in); err != nil || strings.TrimSpace(in.ProxyUsername) == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "proxy_username required"})
		return
	}
	in.ProxyUsername = strings.TrimSpace(in.ProxyUsername)
	in.ExitIP = strings.TrimSpace(in.ExitIP)
	if in.Label == "good" && in.Confidence >= .55 {
		if err := h.service.Admit(c.Request.Context(), in.ProxyUsername, in.ExitIP, in.Confidence, in.ClassifierVersion); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "active"})
		return
	}
	_ = h.service.Evict(c.Request.Context(), in.ProxyUsername)
	if err := h.service.MarkBad(c.Request.Context(), in.ProxyUsername, in.ExitIP, in.Label); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "removed"})
}
