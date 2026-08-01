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

type resultRequest struct {
	ProxyUsername     string  `json:"proxy_username"`
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
	if in.Label == "good" && in.Confidence >= .60 {
		if err := h.service.Admit(c.Request.Context(), in.ProxyUsername, in.Confidence, in.ClassifierVersion); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "active"})
		return
	}
	if err := h.service.Evict(c.Request.Context(), in.ProxyUsername); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "removed"})
}
