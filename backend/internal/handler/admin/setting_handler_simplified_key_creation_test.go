package admin

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestUpdateSettingsSimplifiedKeyCreation(t *testing.T) {
	h, repo := newStepUpSwitchTestHandler(t, map[string]string{})
	for _, step := range []struct {
		name   string
		body   map[string]any
		want   bool
		stored string
	}{
		{"default off", map[string]any{}, false, "false"},
		{"enable", map[string]any{"simplified_key_creation_enabled": true}, true, "true"},
		{"old client preserves enabled", map[string]any{"registration_enabled": true}, true, "true"},
		{"disable", map[string]any{"simplified_key_creation_enabled": false}, false, "false"},
	} {
		t.Run(step.name, func(t *testing.T) {
			rec := doUpdateSettings(t, h, step.body, nil)
			require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
			require.Equal(t, step.stored, repo.values[service.SettingKeySimplifiedKeyCreationEnabled])
			var payload struct {
				Data map[string]any `json:"data"`
			}
			require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &payload))
			require.Equal(t, step.want, payload.Data["simplified_key_creation_enabled"])
			settings, err := h.settingService.GetPublicSettings(context.Background())
			require.NoError(t, err)
			require.Equal(t, step.want, settings.SimplifiedKeyCreationEnabled)
		})
	}
}
