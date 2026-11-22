package service

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/adminaccess"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/stretchr/testify/require"
)

type cursorUsageHTTPUpstream struct {
	HTTPUpstream
	do func(*http.Request) (*http.Response, error)
}

func (u cursorUsageHTTPUpstream) Do(req *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	return u.do(req)
}

func TestGetCursorUsageForAccount(t *testing.T) {
	const baseURL = "http://cursor2api:3010/v1"
	account := &Account{
		ID:          7,
		Platform:    PlatformOpenAI,
		Type:        AccountTypeAPIKey,
		Credentials: map[string]any{"base_url": baseURL, "api_key": "relay-key"},
		GroupIDs:    []int64{1},
	}
	var endpoints []string
	var body string
	cfg := &config.Config{}
	cfg.Security.URLAllowlist.AllowInsecureHTTP = true
	gateway := &OpenAIGatewayService{
		cfg: cfg,
		httpUpstream: cursorUsageHTTPUpstream{do: func(req *http.Request) (*http.Response, error) {
			endpoints = append(endpoints, req.URL.String())
			require.Equal(t, http.MethodGet, req.Method)
			require.Equal(t, "Bearer relay-key", req.Header.Get("Authorization"))
			require.True(t, HTTPUpstreamRedirectsDisabled(req.Context()))
			return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(body))}, nil
		}},
	}
	usage := &AccountUsageService{cursorUsageFetcher: gateway.fetchCursorUsage}
	ctx := context.Background()
	_, err := usage.GetUsageForAccount(ctx, account)
	require.Error(t, err)
	require.Empty(t, endpoints)

	account.Extra = map[string]any{"cursor_usage_enabled": true}
	denied := adminaccess.WithPrincipal(ctx, &adminaccess.Principal{Policy: adminaccess.Policy{
		Rules: []adminaccess.Rule{{Permissions: []string{"accounts.usage.read"}, GroupIDs: []int64{2}, AccountTypes: []string{AccountTypeAPIKey}}},
	}})
	_, err = usage.GetUsageForAccount(denied, account, true)
	require.Equal(t, http.StatusNotFound, infraerrors.Code(err))
	require.Empty(t, endpoints)

	allowed := adminaccess.WithPrincipal(ctx, &adminaccess.Principal{Policy: adminaccess.Policy{
		Rules: []adminaccess.Rule{{Permissions: []string{"accounts.usage.read"}, GroupIDs: []int64{1}, AccountTypes: []string{AccountTypeAPIKey}}},
	}})
	body = `{"cursor_models":{"utilization":0,"resets_at":"2026-11-22T06:03:00Z"},"other_models":{},"updated_at":"2026-11-20T08:00:00Z","stale":true}`
	info, err := usage.GetUsageForAccount(allowed, account)
	require.NoError(t, err)
	require.Equal(t, []string{baseURL + "/usage"}, endpoints)
	require.Equal(t, "active", info.Source)
	require.Equal(t, 0.0, *info.CursorUsage.CursorModels.Utilization)
	require.NotNil(t, info.CursorUsage.CursorModels.ResetsAt)
	require.Nil(t, info.CursorUsage.OtherModels)
	require.True(t, info.CursorUsage.Stale)

	body = `{"unified":true,"updated_at":"2026-11-20T08:00:00Z"}`
	info, err = usage.GetUsageForAccount(allowed, account, true)
	require.NoError(t, err)
	require.True(t, info.CursorUsage.Unified)
	require.Equal(t, []string{baseURL + "/usage", baseURL + "/usage?force=true"}, endpoints)

	body = strings.Repeat(" ", cursorUsageMaxBodyBytes) + body
	_, err = usage.GetUsageForAccount(ctx, account)
	require.Equal(t, http.StatusBadGateway, infraerrors.Code(err))
	require.Equal(t, []string{baseURL + "/usage", baseURL + "/usage?force=true", baseURL + "/usage"}, endpoints)

	account.Credentials["base_url"] = baseURL + "?force=false"
	_, err = usage.GetUsageForAccount(allowed, account, true)
	require.Equal(t, http.StatusBadGateway, infraerrors.Code(err))
	require.Len(t, endpoints, 3)
}
