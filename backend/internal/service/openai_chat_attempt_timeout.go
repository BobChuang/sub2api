package service

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
)

const (
	OpenAIChatAttemptTimeoutCode     = "upstream_attempt_timeout"
	OpenAIChatFirstOutputTimeoutCode = "upstream_first_output_timeout"
	OpenAIChatStreamIdleTimeoutCode  = "upstream_stream_idle_timeout"
	OpenAIChatRequestTimeoutCode     = "upstream_request_timeout"
	OpenAIChatRequestTimeoutMessage  = "Upstream request exceeded its total time limit"
)

var errOpenAIChatAttemptTimeout = errors.New("upstream Chat Completions attempt exceeded its time limit")

type chatAttemptTimeoutKey struct{}

type chatAttemptTimeout struct {
	ctx      context.Context
	cancel   context.CancelFunc
	activity *chatStreamActivityTimeout
}

func (s *OpenAIGatewayService) newChatAttemptTimeout(ctx context.Context, c *gin.Context, stream bool) *chatAttemptTimeout {
	if s == nil || s.cfg == nil {
		return nil
	}
	if stream && c != nil {
		if value, ok := c.Get(chatStreamBudgetKey); ok {
			return value.(chatStreamBudget).newAttempt(ctx)
		}
	}
	seconds := s.cfg.Gateway.ChatCompletionsAttemptTimeoutSeconds[getAPIKeyIDFromContext(c)]
	if seconds <= 0 {
		return nil
	}
	// The per-attempt deadline belongs to the upstream, not the client request:
	// expiring it must leave the client alive for the fallback attempt.
	base, release := detachUpstreamContext(ctx)
	release()
	deadlineCtx, cancel := context.WithTimeoutCause(base, time.Duration(seconds)*time.Second, errOpenAIChatAttemptTimeout)
	return &chatAttemptTimeout{ctx: deadlineCtx, cancel: cancel}
}

func chatAttemptTimeoutFromContext(ctx context.Context) *chatAttemptTimeout {
	if ctx == nil {
		return nil
	}
	attempt, _ := ctx.Value(chatAttemptTimeoutKey{}).(*chatAttemptTimeout)
	return attempt
}

func (a *chatAttemptTimeout) timeoutCause(c *gin.Context) error {
	if a == nil || (c != nil && c.Request != nil && c.Request.Context().Err() != nil) {
		return nil
	}
	cause := context.Cause(a.ctx)
	switch cause {
	case errOpenAIChatAttemptTimeout, errOpenAIChatFirstOutputTimeout, errOpenAIChatStreamIdleTimeout, errOpenAIChatRequestTimeout:
		return cause
	default:
		return nil
	}
}

// Reuse the synchronized SSE heartbeat writer while waiting for an answer.
// Transfer its byte count on return so multiple attempts share one consistent
// "no semantic output yet" size, even when a heartbeat already committed 200.
func startChatAttemptKeepalive(c *gin.Context, interval time.Duration) func() {
	stop := startOpenAISSEKeepalive(c, interval)
	value, _ := c.Get(openAICompactSSEKeepaliveKey)
	k, _ := value.(*openAICompactSSEKeepalive)
	return func() {
		stop()
		if k != nil {
			k.mu.Lock()
			written := k.bytes
			k.bytes = 0
			k.mu.Unlock()
			recordOpenAIStreamKeepaliveBytes(c, written)
			c.Set(openAICompactSSEKeepaliveKey, nil)
		}
	}
}

func newChatAttemptTimeoutError(c *gin.Context, account *Account, requestID string, outputStarted bool, cause error) error {
	kind := "failover"
	if outputStarted || cause == errOpenAIChatRequestTimeout {
		kind = "http_error"
	}
	appendOpsUpstreamError(c, OpsUpstreamErrorEvent{
		Platform: account.Platform, AccountID: account.ID, AccountName: account.Name,
		ProxyID: opsUpstreamProxyID(account), ProxyName: opsUpstreamProxyName(account),
		UpstreamStatusCode: http.StatusGatewayTimeout, UpstreamRequestID: requestID,
		Kind: kind, Message: cause.Error(),
	})
	if outputStarted {
		return newOpenAIUpstreamStreamReadError(cause)
	}
	code, message := classifyOpenAIUpstreamStreamReadError(cause)
	body, _ := json.Marshal(map[string]map[string]string{
		"error": {"type": "upstream_error", "code": code, "message": message},
	})
	failoverErr := &UpstreamFailoverError{StatusCode: http.StatusGatewayTimeout, ResponseBody: body}
	if cause == errOpenAIChatRequestTimeout {
		failoverErr.NextAccountAction = NextAccountStop
	}
	return failoverErr
}

// Hold empty role chunks and heartbeats until the upstream produces actual
// output. Otherwise a role-only chunk at 500ms would make a later timeout
// impossible to retry, despite no answer having been delivered.
func chatTimeoutPayloadStartsOutput(payload string) bool {
	payload = strings.TrimSpace(payload)
	if payload == "[DONE]" || !gjson.Valid(payload) {
		return true
	}
	root := gjson.Parse(payload)
	if root.Get("error").Exists() || root.Get("usage").IsObject() {
		return true
	}
	choices := root.Get("choices")
	if !choices.IsArray() {
		return true // Preserve unknown upstream protocol extensions.
	}
	for _, choice := range choices.Array() {
		if choice.Get("finish_reason").String() != "" {
			return true
		}
		if !choice.Get("delta").IsObject() {
			return true
		}
		starts := false
		choice.Get("delta").ForEach(func(key, value gjson.Result) bool {
			switch key.String() {
			case "role":
			case "content", "reasoning_content", "reasoning":
				starts = value.Type != gjson.Null && (value.Type != gjson.String || value.String() != "")
			case "tool_calls":
				starts = !value.IsArray() || len(value.Array()) > 0
			default:
				starts = value.Type != gjson.Null
			}
			return !starts
		})
		if starts {
			return true
		}
	}
	return false
}
