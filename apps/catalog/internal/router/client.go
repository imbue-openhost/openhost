package router

import (
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

var ErrEndpointUnavailable = errors.New("endpoint unavailable")

type Client struct {
	baseURL string
	http    *http.Client
}

type DeployResult struct {
	AppName string
	Status  string
}

type AppStatusResult struct {
	Status string `json:"status"`
	Error  string `json:"error"`
}

type deployResponse struct {
	OK         bool   `json:"ok"`
	AppName    string `json:"app_name"`
	Status     string `json:"status"`
	Error      string `json:"error"`
	Authorize  string `json:"authorize_url"`
	RawMessage string `json:"message"`
}

func NewClient(baseURL string, timeout time.Duration) *Client {
	return &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		http: &http.Client{
			Timeout: timeout,
			CheckRedirect: func(req *http.Request, via []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
	}
}

func (c *Client) Deploy(
	ctx context.Context,
	token string,
	repoURL string,
	appName string,
) (DeployResult, error) {
	if strings.TrimSpace(token) == "" {
		return DeployResult{}, errors.New("router token is empty")
	}

	result, err := c.deployViaAPIAdd(ctx, token, repoURL, appName)
	if err == nil {
		return result, nil
	}
	if !errors.Is(err, ErrEndpointUnavailable) {
		return DeployResult{}, err
	}

	return c.deployViaAddApp(ctx, token, repoURL, appName)
}

func (c *Client) AppStatus(ctx context.Context, token string, appName string) (AppStatusResult, error) {
	u := c.baseURL + "/api/app_status/" + url.PathEscape(appName)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return AppStatusResult{}, fmt.Errorf("create app status request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return AppStatusResult{}, fmt.Errorf("request app status: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if resp.StatusCode >= 400 {
		return AppStatusResult{}, fmt.Errorf("status endpoint returned %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}

	var out AppStatusResult
	if err := json.Unmarshal(body, &out); err != nil {
		return AppStatusResult{}, fmt.Errorf("decode app status response: %w", err)
	}
	return out, nil
}

func (c *Client) AppLogs(ctx context.Context, token string, appName string) (string, error) {
	u := c.baseURL + "/app_logs/" + url.PathEscape(appName)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return "", fmt.Errorf("create app logs request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := c.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("request app logs: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(io.LimitReader(resp.Body, 512*1024))
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("logs endpoint returned %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}

	return string(body), nil
}

func (c *Client) deployViaAPIAdd(
	ctx context.Context,
	token string,
	repoURL string,
	appName string,
) (DeployResult, error) {
	form := url.Values{}
	form.Set("repo_url", repoURL)
	if appName != "" {
		form.Set("app_name", appName)
	}

	body, statusCode, location, err := c.postForm(ctx, token, "/api/add_app", form)
	if err != nil {
		return DeployResult{}, err
	}

	if statusCode == http.StatusNotFound || statusCode == http.StatusMethodNotAllowed {
		return DeployResult{}, ErrEndpointUnavailable
	}
	if statusCode == http.StatusFound && strings.Contains(strings.ToLower(location), "/login") {
		return DeployResult{}, errors.New("router request was redirected to login; token may be invalid or expired")
	}

	out, err := parseDeployResponse(body)
	if err != nil {
		return DeployResult{}, err
	}
	if statusCode >= 400 {
		return DeployResult{}, fmt.Errorf("deploy failed (%d): %s", statusCode, deployErrorMessage(out, string(body)))
	}

	if out.AppName == "" {
		out.AppName = appName
	}
	if out.Status == "" {
		out.Status = "building"
	}

	return DeployResult{AppName: out.AppName, Status: out.Status}, nil
}

func (c *Client) deployViaAddApp(
	ctx context.Context,
	token string,
	repoURL string,
	appName string,
) (DeployResult, error) {
	form := url.Values{}
	form.Set("repo_url", repoURL)
	form.Set("confirmed", "1")
	if appName != "" {
		form.Set("app_name", appName)
	}

	body, statusCode, location, err := c.postForm(ctx, token, "/add_app", form)
	if err != nil {
		return DeployResult{}, err
	}

	if statusCode == http.StatusFound && strings.Contains(strings.ToLower(location), "/login") {
		return DeployResult{}, errors.New("router request was redirected to login; token may be invalid or expired")
	}
	if statusCode >= 400 {
		out, _ := parseDeployResponse(body)
		return DeployResult{}, fmt.Errorf("deploy failed (%d): %s", statusCode, deployErrorMessage(out, string(body)))
	}

	out, err := parseDeployResponse(body)
	if err != nil {
		return DeployResult{}, err
	}
	if out.AppName == "" {
		out.AppName = appName
	}
	if out.Status == "" {
		out.Status = "building"
	}

	return DeployResult{AppName: out.AppName, Status: out.Status}, nil
}

func (c *Client) postForm(
	ctx context.Context,
	token string,
	path string,
	form url.Values,
) ([]byte, int, string, error) {
	u := c.baseURL + path
	req, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		u,
		strings.NewReader(form.Encode()),
	)
	if err != nil {
		return nil, 0, "", fmt.Errorf("create request %s: %w", path, err)
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, 0, "", fmt.Errorf("request %s: %w", path, err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(io.LimitReader(resp.Body, 256*1024))
	return body, resp.StatusCode, resp.Header.Get("Location"), nil
}

func parseDeployResponse(body []byte) (deployResponse, error) {
	var out deployResponse
	if len(strings.TrimSpace(string(body))) == 0 {
		return out, errors.New("router returned empty response body")
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return out, fmt.Errorf("decode deploy response JSON: %w", err)
	}
	return out, nil
}

func deployErrorMessage(resp deployResponse, rawBody string) string {
	if strings.TrimSpace(resp.Error) != "" {
		return strings.TrimSpace(resp.Error)
	}
	if strings.TrimSpace(resp.RawMessage) != "" {
		return strings.TrimSpace(resp.RawMessage)
	}
	trimmed := strings.TrimSpace(rawBody)
	if trimmed != "" {
		return trimmed
	}
	return "unknown deploy error"
}
