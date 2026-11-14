//go:build unit

package service

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestChatAttemptKeepalivesAcrossRetriesAndTerminalError(t *testing.T) {
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest("POST", "/v1/chat/completions", nil)
	for i := 0; i < 2; i++ {
		stop := startChatAttemptKeepalive(c, time.Hour)
		value, _ := c.Get(openAICompactSSEKeepaliveKey)
		keepalive := value.(*openAICompactSSEKeepalive)
		require.True(t, keepalive.beat())
		require.Equal(t, -1, OpenAICompactKeepaliveAdjustedWrittenSize(c))
		stop()
		require.Equal(t, -1, OpenAICompactKeepaliveAdjustedWrittenSize(c))
	}
	writeChatCompletionsError(c, http.StatusBadRequest, "invalid_request_error", "invalid request")
	require.Equal(t, 200, rec.Code)
	require.Equal(t, 2, strings.Count(rec.Body.String(), ": keepalive\n\n"))
	require.Contains(t, rec.Body.String(), "event: error\ndata: {\"error\":")
	require.True(t, IsResponseCommitted(c))
}

func TestChatAttemptPendingVersusSemanticChunks(t *testing.T) {
	for _, tc := range []struct {
		payload string
		output  bool
	}{
		{`{"choices":[{"delta":{"role":"assistant","content":"","reasoning_content":""}}]}`, false},
		{`{"choices":[{"delta":{"content":"answer"}}]}`, true},
		{`{"choices":[{"delta":{"reasoning_content":"thinking"}}]}`, true},
		{`{"choices":[{"delta":{"tool_calls":[{"index":0}]}}]}`, true},
		{`{"choices":[{"delta":{},"finish_reason":"stop"}]}`, true},
		{`{"choices":[],"usage":{"prompt_tokens":1}}`, true},
		{`{"error":{"code":"cyber_policy"}}`, true},
		{`{"choices":[{"message":{"content":"nonstandard answer"}}]}`, true},
		{"[DONE]", true},
	} {
		t.Run(tc.payload, func(t *testing.T) { require.Equal(t, tc.output, chatTimeoutPayloadStartsOutput(tc.payload)) })
	}
}
