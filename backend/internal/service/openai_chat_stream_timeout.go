package service

import (
	"context"
	"errors"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
)

const chatStreamBudgetKey = "openai_chat_completions_stream_budget"

var (
	errOpenAIChatFirstOutputTimeout = errors.New("upstream Chat Completions did not produce output before the first-output deadline")
	errOpenAIChatStreamIdleTimeout  = errors.New("upstream Chat Completions stopped producing output")
	errOpenAIChatRequestTimeout     = errors.New("upstream Chat Completions exceeded the shared request deadline")
)

type chatStreamBudget struct {
	totalDeadline time.Time
	firstOutput   time.Duration
	idle          time.Duration
}

// StartChatCompletionsStreamBudget gives account selection and both upstream
// attempts one deadline. The client context stays separate for stream errors.
func (s *OpenAIGatewayService) StartChatCompletionsStreamBudget(c *gin.Context, apiKeyID int64) (context.Context, context.CancelFunc) {
	ctx := c.Request.Context()
	if s.cfg == nil {
		return ctx, nil
	}
	cfg, ok := s.cfg.Gateway.ChatCompletionsStreamTimeouts[apiKeyID]
	if !ok {
		return ctx, nil
	}
	budget := chatStreamBudget{
		totalDeadline: time.Now().Add(time.Duration(cfg.TotalTimeoutSeconds) * time.Second),
		firstOutput:   time.Duration(cfg.FirstOutputTimeoutSeconds) * time.Second,
		idle:          time.Duration(cfg.IdleTimeoutSeconds) * time.Second,
	}
	c.Set(chatStreamBudgetKey, budget)
	return context.WithDeadlineCause(ctx, budget.totalDeadline, errOpenAIChatRequestTimeout)
}

func ChatCompletionsStreamBudgetExpired(ctx context.Context) bool {
	return context.Cause(ctx) == errOpenAIChatRequestTimeout
}

func (b chatStreamBudget) newAttempt(ctx context.Context) *chatAttemptTimeout {
	base, release := detachUpstreamContext(ctx)
	release()
	totalCtx, cancelTotal := context.WithDeadlineCause(base, b.totalDeadline, errOpenAIChatRequestTimeout)
	attemptCtx, cancelAttempt := context.WithCancelCause(totalCtx)
	activity := &chatStreamActivityTimeout{
		deadline: time.Now().Add(b.firstOutput),
		cause:    errOpenAIChatFirstOutputTimeout,
		cancel:   cancelAttempt,
		idle:     b.idle,
	}
	activity.mu.Lock()
	activity.timer = time.AfterFunc(b.firstOutput, activity.expire)
	activity.mu.Unlock()
	return &chatAttemptTimeout{ctx: attemptCtx, activity: activity, cancel: func() {
		activity.stop()
		cancelAttempt(nil)
		cancelTotal()
	}}
}

type chatStreamActivityTimeout struct {
	mu       sync.Mutex
	timer    *time.Timer
	deadline time.Time
	cause    error
	cancel   context.CancelCauseFunc
	idle     time.Duration
}

// The timer rechecks the latest deadline instead of resetting on every token.
// The lock also resolves races between arriving output and a timer callback.
func (t *chatStreamActivityTimeout) expire() {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.deadline.IsZero() {
		return
	}
	if remaining := time.Until(t.deadline); remaining > 0 {
		t.timer.Reset(remaining)
		return
	}
	t.cancel(t.cause)
	t.deadline = time.Time{}
}

func (t *chatStreamActivityTimeout) observeOutput(now time.Time) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.deadline.IsZero() {
		return
	}
	if !now.Before(t.deadline) {
		t.cancel(t.cause)
		t.deadline = time.Time{}
		return
	}
	t.deadline = now.Add(t.idle)
	// The first-output deadline can be later than the new idle deadline.
	if t.cause == errOpenAIChatFirstOutputTimeout {
		t.timer.Reset(t.idle)
	}
	t.cause = errOpenAIChatStreamIdleTimeout
}

func (t *chatStreamActivityTimeout) stop() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.deadline = time.Time{}
	t.timer.Stop()
}

// IsOpenAIChatTimeoutCode identifies the timed-out attempts in HTTP and SSE.
func IsOpenAIChatTimeoutCode(code string) bool {
	switch code {
	case OpenAIChatAttemptTimeoutCode, OpenAIChatFirstOutputTimeoutCode, OpenAIChatStreamIdleTimeoutCode, OpenAIChatRequestTimeoutCode:
		return true
	default:
		return false
	}
}
