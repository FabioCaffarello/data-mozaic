package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const maxAuthorizationResponseBytes = 4096

// BC04HTTPAuthorizer delegates operation freshness to BC04's durable owner API.
type BC04HTTPAuthorizer struct {
	endpoint string
	token    string
	client   *http.Client
}

func NewBC04HTTPAuthorizer(endpoint, token string) (*BC04HTTPAuthorizer, error) {
	parsed, err := url.Parse(endpoint)
	if err != nil || parsed == nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" || parsed.Path != "/internal/v1/acquisition-operations/admit" {
		return nil, errors.New("BC04 authorization endpoint is invalid")
	}
	if token == "" || len(token) < 32 {
		return nil, errors.New("BC04 authorization credential is invalid")
	}
	return &BC04HTTPAuthorizer{
		endpoint: parsed.String(),
		token:    token,
		client: &http.Client{
			Timeout: 3 * time.Second,
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
	}, nil
}

func (a *BC04HTTPAuthorizer) AuthorizeOperation(ctx context.Context, capability Capability, operationID, targetDigest string) error {
	body, err := json.Marshal(struct {
		Capability   Capability `json:"capability"`
		OperationID  string     `json:"operation_id"`
		TargetDigest string     `json:"target_digest"`
	}{capability, operationID, targetDigest})
	if err != nil || len(body) > maxRequestBytes {
		return errors.New("BC04 authorization request could not be encoded")
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, a.endpoint, bytes.NewReader(body))
	if err != nil {
		return errors.New("BC04 authorization request could not be created")
	}
	request.Header.Set("Authorization", "Bearer "+a.token)
	request.Header.Set("Content-Type", "application/json")
	response, err := a.client.Do(request)
	if err != nil {
		return errors.New("BC04 authorization endpoint unavailable")
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, maxAuthorizationResponseBytes+1))
	if err != nil || len(responseBody) > maxAuthorizationResponseBytes {
		return errors.New("BC04 authorization response invalid")
	}
	if response.StatusCode == http.StatusForbidden || response.StatusCode == http.StatusUnauthorized {
		return ErrOperationDenied
	}
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("BC04 authorization service returned status %d", response.StatusCode)
	}
	var result struct {
		OperationID string `json:"operation_id"`
		AdmittedAt  string `json:"admitted_at"`
	}
	decoder := json.NewDecoder(strings.NewReader(string(responseBody)))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&result) != nil || result.OperationID == "" || result.OperationID != operationID || result.AdmittedAt == "" {
		return errors.New("BC04 authorization response invalid")
	}
	if decoder.Decode(new(any)) != io.EOF {
		return errors.New("BC04 authorization response invalid")
	}
	return nil
}
