package cli

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	_ "net/http/pprof"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/chenyme/grok2api/backend/internal/app"
	"github.com/chenyme/grok2api/backend/internal/infra/config"
	"github.com/chenyme/grok2api/backend/internal/infra/observability"
)

// Run 解析启动参数并运行后端服务。
func Run(args []string) error {
	options, err := parseOptions(args)
	if err != nil {
		return err
	}
	cfg, err := config.Load(options.configPath)
	if err != nil {
		return err
	}
	if options.listen != "" {
		cfg.Server.Listen = options.listen
		if err := cfg.Validate(); err != nil {
			return err
		}
	}
	logger := observability.NewLogger()
	startPprof(logger)
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	application, err := app.New(ctx, cfg, logger)
	if err != nil {
		return err
	}
	defer application.Close()
	return application.Run(ctx)
}

// startPprof 在显式配置时启动独立诊断端口。部署应只把该端口映射到宿主机
// loopback；它与公开 API 路由完全隔离，不经过前端 fallback。
func startPprof(logger *slog.Logger) {
	listen := os.Getenv("GROK2API_PPROF_LISTEN")
	if listen == "" {
		return
	}
	server := &http.Server{Addr: listen, Handler: http.DefaultServeMux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		logger.Info("pprof_listening", "listen", listen, "distribution", "meow")
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("pprof_failed", "error", err)
		}
	}()
}

type runOptions struct {
	configPath string
	listen     string
}

func parseOptions(args []string) (runOptions, error) {
	options := runOptions{configPath: defaultConfigPath()}
	for index := 0; index < len(args); index++ {
		switch args[index] {
		case "--config":
			if index+1 >= len(args) {
				return runOptions{}, errors.New("--config 缺少路径")
			}
			options.configPath = args[index+1]
			index++
		case "--listen":
			if index+1 >= len(args) {
				return runOptions{}, errors.New("--listen 缺少地址")
			}
			options.listen = args[index+1]
			index++
		default:
			return runOptions{}, fmt.Errorf("不支持的启动参数: %s", args[index])
		}
	}
	return options, nil
}

func defaultConfigPath() string {
	for _, candidate := range []string{"config.yaml", filepath.Join("..", "config.yaml")} {
		info, err := os.Stat(candidate)
		if err == nil && !info.IsDir() {
			return candidate
		}
	}
	return "config.yaml"
}
