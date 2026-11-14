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

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

type chatFallbackUpstream struct {
	service.HTTPUpstream
	hits           []int64
	models         []string
	primaryStatus  int
	fallbackStatus int
	primaryStream  string
	transportError bool
	cancel         context.CancelFunc
}

func (u *chatFallbackUpstream) Do(req *http.Request, _ string, id int64, _ int) (*http.Response, error) {
	body, _ := io.ReadAll(req.Body)
	u.hits = append(u.hits, id)
	u.models = append(u.models, gjson.GetBytes(body, "model").String())
	if id != 353 && u.cancel != nil {
		u.cancel()
		return nil, context.Canceled
	}
	if id != 353 && u.transportError {
		return nil, errors.New("upstream connection reset")
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
	for _, tc := range []struct {
		name                                                                                          string
		primary, fallback                                                                             int
		stream, transport, disabled, unavailable, wrongModel, wrongGroup, canceled, fallbackInitially bool
		primaryStream                                                                                 string
		wantHits                                                                                      []int64
		wantStatus                                                                                    int
		wantStreamError                                                                               bool
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
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := &config.Config{RunMode: config.RunModeSimple}
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
			upstream := &chatFallbackUpstream{primaryStatus: tc.primary, fallbackStatus: tc.fallback, primaryStream: tc.primaryStream, transportError: tc.transport}
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
			}

		})
	}
}
