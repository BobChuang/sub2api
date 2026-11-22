package service

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
)

const cursorUsageMaxBodyBytes = 16 * 1024

var errCursorUsageUnavailable = infraerrors.New(http.StatusBadGateway, "CURSOR_USAGE_UNAVAILABLE", "Cursor usage is unavailable")

type CursorUsageSnapshot struct {
	CursorModels *CursorUsageWindow `json:"cursor_models,omitempty"`
	OtherModels  *CursorUsageWindow `json:"other_models,omitempty"`
	GrokBot      *CursorUsageWindow `json:"grok_bot,omitempty"`
	UpdatedAt    time.Time          `json:"updated_at"`
	Stale        bool               `json:"stale,omitempty"`
	Unified      bool               `json:"unified,omitempty"`
}

type CursorUsageWindow struct {
	Utilization *float64   `json:"utilization"`
	ResetsAt    *time.Time `json:"resets_at,omitempty"`
}

func cursorUsageEnabled(account *Account) bool {
	return account != nil && account.IsOpenAIApiKey() && !account.IsShadow() && account.Extra["cursor_usage_enabled"] == true
}

// fetchCursorUsage accesses the opted-in relay using its existing API key. The
// Cursor login stays on the relay, and usage failures do not change routing.
func (s *OpenAIGatewayService) fetchCursorUsage(ctx context.Context, account *Account, forceProbe bool) (*CursorUsageSnapshot, error) {
	if s == nil || s.httpUpstream == nil || !cursorUsageEnabled(account) {
		return nil, errCursorUsageUnavailable
	}
	apiKey := strings.TrimSpace(account.GetOpenAIApiKey())
	baseURL := strings.TrimSpace(account.GetCredential("base_url"))
	if apiKey == "" || baseURL == "" {
		return nil, errCursorUsageUnavailable
	}
	baseURL, err := s.validateUpstreamBaseURL(baseURL)
	if err != nil {
		return nil, errCursorUsageUnavailable
	}
	if account.ProxyID != nil && (account.Proxy == nil || account.Proxy.ID != *account.ProxyID) {
		return nil, errCursorUsageUnavailable
	}

	ctx, cancel := context.WithTimeout(WithHTTPUpstreamRedirectsDisabled(ctx), 12*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, buildOpenAIEndpointURL(baseURL, "/v1/usage"), nil)
	if err != nil || req.URL.User != nil || req.URL.RawQuery != "" {
		return nil, errCursorUsageUnavailable
	}
	if forceProbe {
		req.URL.RawQuery = "force=true"
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Authorization", "Bearer "+apiKey)
	resp, err := s.httpUpstream.Do(req, upstreamModelsProxyURL(account), account.ID, account.Concurrency)
	if err != nil || resp == nil || resp.Body == nil {
		return nil, errCursorUsageUnavailable
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, errCursorUsageUnavailable
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, cursorUsageMaxBodyBytes+1))
	if err != nil || len(body) > cursorUsageMaxBodyBytes {
		return nil, errCursorUsageUnavailable
	}
	var usage CursorUsageSnapshot
	if err := json.Unmarshal(body, &usage); err != nil || usage.UpdatedAt.IsZero() {
		return nil, errCursorUsageUnavailable
	}
	usage.CursorModels = usableCursorUsageWindow(usage.CursorModels)
	usage.OtherModels = usableCursorUsageWindow(usage.OtherModels)
	usage.GrokBot = usableCursorUsageWindow(usage.GrokBot)
	if !usage.Unified && usage.CursorModels == nil && usage.OtherModels == nil && usage.GrokBot == nil {
		return nil, errCursorUsageUnavailable
	}
	return &usage, nil
}

func usableCursorUsageWindow(window *CursorUsageWindow) *CursorUsageWindow {
	if window == nil || window.Utilization == nil || *window.Utilization < 0 {
		return nil
	}
	return window
}
