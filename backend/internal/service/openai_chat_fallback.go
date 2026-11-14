package service

import (
	"context"
	"fmt"
)

// SelectChatCompletionsFallbackAccount pins a retry to the configured account
// while retaining the regular group, model, health, pricing and concurrency
// checks. It deliberately does not bind the session to the fallback account.
func (s *OpenAIGatewayService) SelectChatCompletionsFallbackAccount(
	ctx context.Context, groupID *int64, requestedModel string, accountID int64, platform string,
) (*AccountSelectionResult, OpenAIAccountScheduleDecision, error) {
	decision := OpenAIAccountScheduleDecision{}
	accounts, err := s.listSchedulableAccounts(ctx, groupID, platform)
	if err != nil {
		return nil, decision, err
	}
	excluded := make(map[int64]struct{}, len(accounts))
	found := false
	for i := range accounts {
		if accounts[i].ID == accountID && s.openAIAccountMatchesSchedulingGroup(&accounts[i], groupID) {
			found = true
		} else {
			excluded[accounts[i].ID] = struct{}{}
		}
	}
	if accountID <= 0 || !found {
		return nil, decision, fmt.Errorf("%w: configured chat fallback account is unavailable", ErrNoAvailableAccounts)
	}
	selection, decision, err := s.SelectAccountWithSchedulerForCapability(
		ctx, groupID, "", "", requestedModel, excluded,
		OpenAIUpstreamTransportAny, OpenAIEndpointCapabilityChatCompletions,
		false, false, true, platform,
	)
	if err != nil {
		return nil, decision, err
	}
	// A concurrently added account must never turn an explicit retry into a
	// selection from the primary pool.
	if selection == nil || selection.Account == nil || selection.Account.ID != accountID {
		if selection != nil && selection.ReleaseFunc != nil {
			selection.ReleaseFunc()
		}
		return nil, decision, fmt.Errorf("%w: configured chat fallback account was not selected", ErrNoAvailableAccounts)
	}
	decision.Layer = "explicit_chat_fallback"
	return selection, decision, nil
}
