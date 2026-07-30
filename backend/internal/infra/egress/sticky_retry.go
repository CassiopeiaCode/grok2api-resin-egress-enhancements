package egress

import (
	"bytes"
	"crypto/tls"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"strings"
)

// do retries only the connection phase of a proxied request.
// Once the request is written to the upstream tunnel, replaying a POST could
// duplicate generation or billing and is therefore never attempted here.
func (l *Lease) do(request *http.Request) (*http.Response, error) {
	if l == nil || l.client == nil {
		return nil, errors.New("出口客户端未初始化")
	}
	if strings.TrimSpace(l.ProxyURL) == "" || l.reconnect == nil {
		return l.client.Do(request)
	}
	current := request
	for attempt := 0; ; attempt++ {
		written := false
		trace := &httptrace.ClientTrace{WroteRequest: func(httptrace.WroteRequestInfo) {
			// The callback also fires when writing fails after a partial write;
			// treat that as submitted because the upstream may have received it.
			written = true
		}}
		traced := current.WithContext(httptrace.WithClientTrace(current.Context(), trace))
		response, err := l.client.Do(traced)
		proxyResponseFailure := retryableProxyConnectionResponse(response)
		if err == nil && !proxyResponseFailure {
			return response, nil
		}
		connectionFailure := safeProxyConnectionFailure(err, response)
		// 带有可信代理连接失败标记的响应表示请求没有到达上游，即使本地已经
		// 写入代理隧道也可以安全重放；普通写后错误仍禁止重放。
		if attempt >= stickyProxyRetryLimit || (!proxyResponseFailure && written) || (!connectionFailure && !proxyResponseFailure) {
			if err != nil {
				return nil, err
			}
			return response, nil
		}
		next, cloneErr := cloneRequestBody(request)
		if cloneErr != nil {
			if err != nil {
				return nil, err
			}
			return response, nil
		}
		if l.reconnect == nil {
			if err != nil {
				return nil, err
			}
			return response, nil
		}
		failedNodeID := l.NodeID
		previousUserAgent, previousCookies := l.UserAgent, l.CFCookies
		l.client.CloseIdleConnections()
		nextLease, reconnectErr := l.reconnect(request.Context(), failedNodeID)
		if reconnectErr != nil {
			if err != nil {
				return nil, err
			}
			return response, nil
		}
		if response != nil && response.Body != nil {
			_ = response.Body.Close()
		}
		l.adopt(nextLease)
		refreshProxyHeaders(next, previousUserAgent, previousCookies, l.UserAgent, l.CFCookies)
		current = next
	}
}

func refreshProxyHeaders(request *http.Request, previousUserAgent, previousCookies, nextUserAgent, nextCookies string) {
	if request == nil {
		return
	}
	if strings.TrimSpace(nextUserAgent) != "" && (request.Header.Get("User-Agent") == previousUserAgent || strings.TrimSpace(request.Header.Get("User-Agent")) == "") {
		request.Header.Set("User-Agent", nextUserAgent)
	}
	if previousCookies != nextCookies && strings.TrimSpace(previousCookies) != "" {
		request.Header.Set("Cookie", strings.ReplaceAll(request.Header.Get("Cookie"), previousCookies, nextCookies))
	}
}

func (l *Lease) adopt(next *Lease) {
	if l == nil || next == nil {
		return
	}
	l.Release()
	l.NodeID, l.NodeName, l.Scope = next.NodeID, next.NodeName, next.Scope
	l.ProxyURL, l.UserAgent, l.CFCookies = next.ProxyURL, next.UserAgent, next.CFCookies
	l.client, l.browser, l.sticky = next.client, next.browser, next.sticky
	l.release, l.reconnect = next.release, next.reconnect
	next.release = nil
}

func cloneRequestBody(request *http.Request) (*http.Request, error) {
	if request == nil {
		return nil, errors.New("请求为空")
	}
	if request.Body == nil || request.Body == http.NoBody {
		return request.Clone(request.Context()), nil
	}
	if request.GetBody == nil {
		return nil, errors.New("请求体不可重放")
	}
	body, err := request.GetBody()
	if err != nil {
		return nil, err
	}
	cloned := request.Clone(request.Context())
	cloned.Body = body
	return cloned, nil
}

func safeProxyConnectionFailure(err error, response *http.Response) bool {
	if response != nil {
		resinError := strings.ToUpper(strings.TrimSpace(response.Header.Get("X-Resin-Error")))
		return response.StatusCode >= http.StatusBadGateway && (resinError == "UPSTREAM_CONNECT_FAILED" || resinError == "NO_AVAILABLE_NODES")
	}
	if err == nil {
		return false
	}
	value := strings.ToLower(err.Error())
	for _, marker := range []string{
		"proxyconnect", "socks connect", "socks5: authentication", "tls handshake timeout",
		"connection refused", "no route to host", "i/o timeout",
	} {
		if strings.Contains(value, marker) {
			return true
		}
	}
	var operationError *net.OpError
	if errors.As(err, &operationError) && (operationError.Op == "dial" || operationError.Op == "proxyconnect") {
		return true
	}
	var tlsError *tls.RecordHeaderError
	return errors.As(err, &tlsError)
}

func retryableResinResponse(response *http.Response) bool {
	if response == nil {
		return false
	}
	resinError := strings.ToUpper(strings.TrimSpace(response.Header.Get("X-Resin-Error")))
	return (resinError == "UPSTREAM_CONNECT_FAILED" || resinError == "NO_AVAILABLE_NODES") && response.StatusCode >= 502
}

// retryableProxyConnectionResponse 只识别代理明确声明的连接建立失败。
// 读取后会完整恢复 Body，非重试路径的调用方仍能看到原始响应。
func retryableProxyConnectionResponse(response *http.Response) bool {
	if response == nil || response.StatusCode < http.StatusInternalServerError {
		return false
	}
	if retryableResinResponse(response) {
		return true
	}
	if response.StatusCode != http.StatusInternalServerError || response.Body == nil {
		return false
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, 64<<10))
	if err != nil {
		return false
	}
	response.Body = &replayReadCloser{Reader: io.MultiReader(bytes.NewReader(data), response.Body), Closer: response.Body}
	value := strings.ToLower(string(data))
	return strings.Contains(value, "连接上游服务失败") ||
		strings.Contains(value, "upstream_connect_failed") ||
		strings.Contains(value, "upstream connection failed") ||
		strings.Contains(value, "failed to connect to upstream")
}

type replayReadCloser struct {
	io.Reader
	io.Closer
}
