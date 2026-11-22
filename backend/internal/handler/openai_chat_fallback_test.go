//go:build unit

package handler

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

type chatFallbackUpstream struct {
	service.HTTPUpstream
	hits                  []int64
	models                []string
	primaryStatus         int
	fallbackStatus        int
	primaryStream         string
	transportError        bool
	cancel                context.CancelFunc
	holdHeaders           bool
	holdBody              bool
	holdFallback          bool
	deadlines             []time.Time
	primaryStreamInterval time.Duration
}

func (u *chatFallbackUpstream) Do(req *http.Request, _ string, id int64, _ int) (*http.Response, error) {
	body, _ := io.ReadAll(req.Body)
	u.hits = append(u.hits, id)
	u.models = append(u.models, gjson.GetBytes(body, "model").String())
	deadline, _ := req.Context().Deadline()
	u.deadlines = append(u.deadlines, deadline)
	if id != 353 && u.cancel != nil {
		u.cancel()
		return nil, context.Canceled
	}
	if id != 353 && u.transportError {
		return nil, errors.New("upstream connection reset")
	}
	if (id != 353 && u.holdHeaders) || (id == 353 && u.holdFallback) {
		<-req.Context().Done()
		return nil, req.Context().Err()
	}
	if id != 353 && u.holdBody {
		reader, writer := io.Pipe()
		go func() {
			defer writer.Close()
			if u.primaryStream != "" {
				if _, err := io.WriteString(writer, u.primaryStream); err != nil {
					return
				}
			}
			if u.primaryStreamInterval <= 0 {
				<-req.Context().Done()
				return
			}
			ticker := time.NewTicker(u.primaryStreamInterval)
			defer ticker.Stop()
			for {
				select {
				case <-req.Context().Done():
					return
				case <-ticker.C:
					if _, err := io.WriteString(writer, u.primaryStream); err != nil {
						return
					}
				}
			}
		}()
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: reader}, nil
	}
	status := u.primaryStatus
	if id == 353 {
		status = u.fallbackStatus
	}
	if status >= 400 {
		return &http.Response{StatusCode: status, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(`{"error":{"message":"upstream unavailable"}}`))}, nil
	}
	if gjson.GetBytes(body, "stream").Bool() {
		stream := u.primaryStream
		if id == 353 {
			stream = "data: {\"choices\":[{\"delta\":{\"content\":\"fallback answer\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":1,\"completion_tokens\":2}}\n\ndata: [DONE]\n\n"
		}
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: io.NopCloser(strings.NewReader(stream))}, nil
	}
	return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"id":"test","choices":[{"message":{"role":"assistant","content":"answer"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":2}}`))}, nil
}

func TestChatCompletionsExplicitFallback(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const rolePrefix = "data: {\"id\":\"primary-marker\",\"choices\":[{\"delta\":{\"role\":\"assistant\",\"content\":\"\"},\"finish_reason\":null}]}\n\n"
	const partialOutput = "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n"
	for _, tc := range []struct {
		name                                                                                          string
		primary, fallback                                                                             int
		stream, transport, disabled, unavailable, wrongModel, wrongGroup, canceled, fallbackInitially bool
		primaryStream                                                                                 string
		wantHits                                                                                      []int64
		wantStatus                                                                                    int
		timeoutSeconds                                                                                int
		holdHeaders, holdBody, holdFallback, wantStreamError, wantHeartbeat                           bool
		streamTimeout                                                                                 config.ChatCompletionsStreamTimeoutConfig
		streamTimeoutKey                                                                              int64
		primaryStreamInterval                                                                         time.Duration
		wantTimeoutCode, wantOutput                                                                   string
		wantPromptTerminal                                                                            bool
	}{
		{name: "429 skips remaining primary accounts", primary: 429, wantHits: []int64{801, 353}, wantStatus: 200},
		{name: "500 uses reserved attempt with zero switch budget", primary: 500, wantHits: []int64{801, 353}, wantStatus: 200},
		{name: "503 uses fallback", primary: 503, wantHits: []int64{801, 353}, wantStatus: 200},
		{name: "credential failure uses fallback", primary: 401, wantHits: []int64{801, 353}, wantStatus: 200},
		{name: "transport failure uses fallback", transport: true, wantHits: []int64{801, 353}, wantStatus: 200},
		{name: "streaming 429 uses fallback", primary: 429, stream: true, wantHits: []int64{801, 353}, wantStatus: 200},
		{name: "empty stream uses fallback", stream: true, wantHits: []int64{801, 353}, wantStatus: 200},
		{name: "partial stream never concatenates fallback", stream: true, primaryStream: "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n", wantStreamError: true, wantHits: []int64{801}, wantStatus: 200},
		{name: "fallback failure terminates", primary: 429, fallback: 503, wantHits: []int64{801, 353}, wantStatus: 502},
		{name: "initial fallback is never replayed", fallback: 429, fallbackInitially: true, wantHits: []int64{353}, wantStatus: 429},
		{name: "disabled key retains switch budget", primary: 429, disabled: true, wantHits: []int64{801}, wantStatus: 429},
		{name: "unavailable fallback never picks another primary", primary: 429, unavailable: true, wantHits: []int64{801}, wantStatus: 429},
		{name: "unsupported model never bypasses eligibility", primary: 429, wrongModel: true, wantHits: []int64{801}, wantStatus: 429},
		{name: "fallback outside key group is rejected", primary: 429, wrongGroup: true, wantHits: []int64{801}, wantStatus: 429},
		{name: "client cancellation never retries", canceled: true, wantHits: []int64{801}, wantStatus: 200},
		{name: "healthy primary never calls fallback", wantHits: []int64{801}, wantStatus: 200},
		{name: "header timeout uses fallback", timeoutSeconds: 1, holdHeaders: true, wantHits: []int64{801, 353}, wantStatus: 200},
		{name: "nonstream body timeout uses fallback", timeoutSeconds: 1, holdBody: true, wantHits: []int64{801, 353}, wantStatus: 200},
		{name: "empty role prefix remains replayable", timeoutSeconds: 1, holdBody: true, stream: true, primaryStream: "data: {\"id\":\"primary-marker\",\"choices\":[{\"delta\":{\"role\":\"assistant\",\"content\":\"\"},\"finish_reason\":null}]}\n\n", wantHits: []int64{801, 353}, wantStatus: 200},
		{name: "heartbeats do not prevent fallback", timeoutSeconds: 2, holdBody: true, stream: true, wantHeartbeat: true, wantHits: []int64{801, 353}, wantStatus: 200},
		{name: "content timeout fails without replay", timeoutSeconds: 1, holdBody: true, stream: true, primaryStream: "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n", wantStreamError: true, wantHits: []int64{801}, wantStatus: 200},
		{name: "reasoning timeout fails without replay", timeoutSeconds: 1, holdBody: true, stream: true, primaryStream: "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"thinking\"}}]}\n\n", wantStreamError: true, wantHits: []int64{801}, wantStatus: 200},
		{name: "tool call timeout fails without replay", timeoutSeconds: 1, holdBody: true, stream: true, primaryStream: "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"function\":{\"name\":\"run\"}}]}}]}\n\n", wantStreamError: true, wantHits: []int64{801}, wantStatus: 200},
		{name: "fallback receives its own bounded deadline", timeoutSeconds: 1, holdHeaders: true, holdFallback: true, wantHits: []int64{801, 353}, wantStatus: 504},
		{name: "exhausted timeout after heartbeat counts as failure", timeoutSeconds: 2, stream: true, wantHeartbeat: true, holdHeaders: true, holdFallback: true, wantHits: []int64{801, 353}, wantStatus: 200},
		{name: "client cancellation with deadline never retries", timeoutSeconds: 1, canceled: true, wantHits: []int64{801}, wantStatus: 200},
		{name: "healthy primary with deadline never retries", timeoutSeconds: 1, wantHits: []int64{801}, wantStatus: 200},
		{name: "empty terminal stream completes before deadline", timeoutSeconds: 1, stream: true, primaryStream: "data: {\"choices\":[{\"delta\":{\"role\":\"assistant\",\"content\":\"\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n", wantHits: []int64{801}, wantStatus: 200},
		{
			name: "first output timeout ignores role and heartbeats and switches once", stream: true, holdBody: true,
			streamTimeout: config.ChatCompletionsStreamTimeoutConfig{FirstOutputTimeoutSeconds: 2, IdleTimeoutSeconds: 1, TotalTimeoutSeconds: 4},
			primaryStream: rolePrefix, wantHeartbeat: true, wantOutput: "fallback answer", wantHits: []int64{801, 353}, wantStatus: 200,
		},
		{
			name: "retryable upstream SSE error before output switches once", stream: true,
			streamTimeout: config.ChatCompletionsStreamTimeoutConfig{FirstOutputTimeoutSeconds: 2, IdleTimeoutSeconds: 1, TotalTimeoutSeconds: 4},
			primaryStream: "data: {\"error\":{\"message\":\"temporarily unavailable\",\"type\":\"server_error\"}}\n\ndata: [DONE]\n\n",
			wantOutput:    "fallback answer", wantHits: []int64{801, 353}, wantStatus: 200,
		},
		{
			name: "invalid request SSE error before output retains its original response", stream: true,
			streamTimeout: config.ChatCompletionsStreamTimeoutConfig{FirstOutputTimeoutSeconds: 2, IdleTimeoutSeconds: 1, TotalTimeoutSeconds: 4},
			primaryStream: "data: {\"error\":{\"message\":\"invalid request\",\"type\":\"invalid_request_error\"}}\n\ndata: [DONE]\n\n",
			wantOutput:    "invalid_request_error", wantHits: []int64{801}, wantStatus: 200,
		},
		{
			name: "idle after output sends an SSE error without replay", stream: true, holdBody: true,
			streamTimeout: config.ChatCompletionsStreamTimeoutConfig{FirstOutputTimeoutSeconds: 2, IdleTimeoutSeconds: 1, TotalTimeoutSeconds: 4},
			primaryStream: partialOutput, wantStreamError: true, wantOutput: "partial", wantTimeoutCode: "upstream_stream_idle_timeout", wantHits: []int64{801}, wantStatus: 200,
		},
		{
			name: "fallback shares the total budget with the first attempt", stream: true, holdHeaders: true, holdFallback: true,
			streamTimeout:   config.ChatCompletionsStreamTimeoutConfig{FirstOutputTimeoutSeconds: 2, IdleTimeoutSeconds: 1, TotalTimeoutSeconds: 3},
			wantTimeoutCode: "upstream_request_timeout", wantHits: []int64{801, 353}, wantStatus: 504,
		},
		{
			name: "total timeout applies even while output keeps arriving", stream: true, holdBody: true,
			streamTimeout: config.ChatCompletionsStreamTimeoutConfig{FirstOutputTimeoutSeconds: 2, IdleTimeoutSeconds: 1, TotalTimeoutSeconds: 3},
			primaryStream: partialOutput, primaryStreamInterval: 250 * time.Millisecond, wantStreamError: true, wantOutput: "partial", wantTimeoutCode: "upstream_request_timeout", wantHits: []int64{801}, wantStatus: 200,
		},
		{
			name: "DONE completes the stream before the upstream closes", stream: true, holdBody: true,
			streamTimeout: config.ChatCompletionsStreamTimeoutConfig{FirstOutputTimeoutSeconds: 2, IdleTimeoutSeconds: 3, TotalTimeoutSeconds: 6},
			primaryStream: partialOutput + "data: [DONE]\n\n", wantPromptTerminal: true, wantHits: []int64{801}, wantStatus: 200,
		},
		{
			name: "another key retains the old timeout", stream: true, holdBody: true, timeoutSeconds: 1,
			streamTimeout:    config.ChatCompletionsStreamTimeoutConfig{FirstOutputTimeoutSeconds: 2, IdleTimeoutSeconds: 2, TotalTimeoutSeconds: 4},
			streamTimeoutKey: 10, primaryStream: partialOutput, wantStreamError: true, wantHits: []int64{801}, wantStatus: 200,
		},
		{
			name: "nonstream retains the old timeout for the configured key", holdBody: true, timeoutSeconds: 1,
			streamTimeout: config.ChatCompletionsStreamTimeoutConfig{FirstOutputTimeoutSeconds: 2, IdleTimeoutSeconds: 2, TotalTimeoutSeconds: 4},
			wantHits:      []int64{801, 353}, wantStatus: 200,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := &config.Config{RunMode: config.RunModeSimple}
			if tc.timeoutSeconds > 0 {
				cfg.Gateway.ChatCompletionsAttemptTimeoutSeconds = map[int64]int{9: tc.timeoutSeconds}
			}
			if tc.streamTimeout.TotalTimeoutSeconds > 0 {
				streamTimeoutKey := tc.streamTimeoutKey
				if streamTimeoutKey == 0 {
					streamTimeoutKey = 9
				}
				cfg.Gateway.ChatCompletionsStreamTimeouts = map[int64]config.ChatCompletionsStreamTimeoutConfig{streamTimeoutKey: tc.streamTimeout}
			}
			if tc.wantHeartbeat {
				cfg.Gateway.StreamKeepaliveInterval = 1
			}
			if tc.wrongGroup {
				cfg.RunMode = config.RunModeStandard
			}
			if !tc.disabled {
				cfg.Gateway.ChatCompletionsFallbackAccounts = map[int64]int64{9: 353}
			} else {
				cfg.Gateway.ChatCompletionsFallbackAccounts = map[int64]int64{10: 353}
			}
			groupID := int64(10)
			accounts := []service.Account{}
			for _, id := range []int64{801, 802, 803, 804, 353} {
				priority := 1
				baseURL := "https://api.cline.bot/api/v1"
				if id == 353 {
					priority = 100
					baseURL = "https://api.deepseek.com/v1"
				}
				accounts = append(accounts, service.Account{ID: id, Platform: service.PlatformCline, Type: service.AccountTypeAPIKey, Status: service.StatusActive, Schedulable: true, Priority: priority, Concurrency: 3,
					Credentials:   map[string]any{"api_key": "test", "base_url": baseURL, "model_mapping": map[string]any{"deepseek-v4.1-flash": "deepseek-flash"}},
					AccountGroups: []service.AccountGroup{{AccountID: id, GroupID: groupID}},
				})
			}
			if tc.unavailable {
				accounts[len(accounts)-1].Schedulable = false
			}
			if tc.fallbackInitially {
				for i := range accounts[:len(accounts)-1] {
					accounts[i].Schedulable = false
				}
			}
			if tc.wrongModel {
				accounts[len(accounts)-1].Credentials["model_mapping"] = map[string]any{"other-model": "other-model"}
			}
			if tc.wrongGroup {
				accounts[len(accounts)-1].AccountGroups = []service.AccountGroup{{AccountID: 353, GroupID: 11}}
			}
			repo := &grokCredentialHandlerRepo{accounts: accounts}
			upstream := &chatFallbackUpstream{primaryStatus: tc.primary, fallbackStatus: tc.fallback, primaryStream: tc.primaryStream, transportError: tc.transport, holdHeaders: tc.holdHeaders, holdBody: tc.holdBody, holdFallback: tc.holdFallback, primaryStreamInterval: tc.primaryStreamInterval}
			billingCache := service.NewBillingCacheService(newHandlerInflightCache(100), nil, nil, nil, nil, nil, cfg, nil)
			t.Cleanup(billingCache.Stop)
			gateway := service.NewOpenAIGatewayService(repo, nil, nil, nil, nil, nil, nil, cfg, nil, nil, service.NewBillingService(cfg, nil), nil, billingCache, upstream, &service.DeferredService{}, nil, nil, nil, nil, nil, nil, nil)
			cache := &concurrencyCacheMock{acquireUserSlotFn: func(context.Context, int64, int, string) (bool, error) { return true, nil }, acquireAccountSlotFn: func(context.Context, int64, int, string) (bool, error) { return true, nil }}
			h := NewOpenAIGatewayHandler(gateway, service.NewConcurrencyService(cache), billingCache, &service.APIKeyService{}, nil, nil, nil, nil, cfg)
			h.maxAccountSwitches = 0
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			stream := "false"
			if tc.stream {
				stream = "true"
			}
			c.Request = httptest.NewRequest("POST", "/v1/chat/completions", strings.NewReader(`{"model":"deepseek-v4.1-flash","stream":`+stream+`,"messages":[{"role":"user","content":"hi"}]}`))
			if tc.canceled {
				ctx, cancel := context.WithCancel(c.Request.Context())
				defer cancel()
				c.Request = c.Request.WithContext(ctx)
				upstream.cancel = cancel
			}
			key := &service.APIKey{ID: 9, GroupID: &groupID, User: &service.User{ID: 1, Balance: 100, Status: service.StatusActive}, Group: &service.Group{ID: groupID, Hydrated: true, Platform: service.PlatformCline, Status: service.StatusActive}}
			c.Set(string(middleware.ContextKeyAPIKey), key)
			c.Set(string(middleware.ContextKeyUser), middleware.AuthSubject{UserID: 1, Concurrency: 1})
			started := time.Now()
			h.ChatCompletions(c)
			require.Equal(t, tc.wantHits, upstream.hits, rec.Body.String())
			require.Equal(t, tc.wantStatus, rec.Code, rec.Body.String())
			for i, id := range upstream.hits {
				if id == 353 {
					require.Equal(t, "deepseek-flash", upstream.models[i])
				}
			}
			if tc.wantStreamError {
				require.NotContains(t, rec.Body.String(), "fallback answer")
				require.Contains(t, rec.Body.String(), `"error"`)
				if tc.timeoutSeconds > 0 {
					require.Contains(t, rec.Body.String(), service.OpenAIChatAttemptTimeoutCode)
				}
			}
			if tc.holdFallback && tc.timeoutSeconds > 0 {
				require.True(t, upstream.deadlines[1].After(upstream.deadlines[0].Add(900*time.Millisecond)))
				require.Contains(t, rec.Body.String(), service.OpenAIChatAttemptTimeoutCode)
			}
			if tc.wantTimeoutCode != "" {
				require.Contains(t, rec.Body.String(), tc.wantTimeoutCode)
				if tc.wantStreamError {
					require.Contains(t, rec.Body.String(), "event: error\ndata: ")
				}
			}
			if tc.stream && (tc.timeoutSeconds > 0 || tc.wantTimeoutCode != "") && (tc.wantStreamError || tc.holdFallback) && rec.Code == 200 {
				errors := service.GetOpsStreamErrors(c)
				require.Len(t, errors, 1)
				require.True(t, errors[0].CountTowardsSLA)
				require.Equal(t, http.StatusGatewayTimeout, errors[0].IntendedStatus)
				if tc.wantTimeoutCode != "" {
					require.Equal(t, tc.wantTimeoutCode, errors[0].Code)
				}
			}
			if tc.wantHeartbeat {
				require.Contains(t, rec.Body.String(), ": keepalive\n\n")
			}
			if len(upstream.hits) == 2 && tc.holdBody {
				require.NotContains(t, rec.Body.String(), "primary-marker")
			}
			if tc.wantOutput != "" {
				require.Contains(t, rec.Body.String(), tc.wantOutput)
			}
			if tc.wantPromptTerminal {
				require.True(t, strings.HasSuffix(rec.Body.String(), "data: [DONE]\n\n"), rec.Body.String())
				require.Less(t, time.Since(started), 2*time.Second)
			}
		})
	}
}
