package web

import (
	"context"
	"crypto/rand"
	"database/sql"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"html/template"
	"io"
	"io/fs"
	"log"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/imbue-openhost/openhost-catalog/internal/catalog"
	"github.com/imbue-openhost/openhost-catalog/internal/config"
	"github.com/imbue-openhost/openhost-catalog/internal/router"
	"github.com/imbue-openhost/openhost-catalog/internal/store"
)

//go:embed templates/*.html static/*
var assets embed.FS

var appNamePattern = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`)

// sourceSyncBudget caps how long a single page load can spend syncing all
// enabled sources before we give up and render with whatever data we have.
const sourceSyncBudget = 10 * time.Second

type Server struct {
	cfg      config.Config
	store    *store.Store
	catalog  *catalog.Service
	router   *router.Client
	http     *http.Client
	tmpl     *template.Template
	static   http.Handler
	tokenMu  sync.Mutex
	tokenVal string
	tokenTS  time.Time
}

type indexPageData struct {
	BasePath        string
	Query           string
	SourceFilter    string
	TagFilter       string
	Sources         []store.Source
	Apps            []store.CatalogApp
	Error           string
	CanDeploy       bool
	RouterBaseURL   string
	FailedSyncNames []string
}

type appPageData struct {
	BasePath      string
	App           store.CatalogApp
	Error         string
	CanDeploy     bool
	RouterBaseURL string
}

type sourcesPageData struct {
	BasePath string
	Sources  []store.Source
	Message  string
	Error    string
}

type setupPageData struct {
	BasePath          string
	Error             string
	CurrentMode       string // "", "deploy", or "manual"
	SecretsMissing    bool   // true if the last deploy attempt failed because the Secrets app is not installed
	SecretsInstallURL string // URL to install the Secrets app on the OpenHost dashboard
}

// ErrSecretsAppMissing is returned when the router's secrets service is unreachable
// because the Secrets app is not installed on the OpenHost instance.
var ErrSecretsAppMissing = errors.New("the Secrets app is not installed on this OpenHost instance")

type publishPageData struct {
	BasePath     string
	Publish      store.Publish
	Terminal     bool
	RouterAppURL string
	RouterPage   string
}

type publishStatusResponse struct {
	ID               string `json:"id"`
	Status           string `json:"status"`
	ErrorMessage     string `json:"error_message"`
	RouterAppName    string `json:"router_app_name"`
	ManualInstallURL string `json:"manual_install_url"`
	Terminal         bool   `json:"terminal"`
	RouterAppURL     string `json:"router_app_url,omitempty"`
	RouterPageURL    string `json:"router_page_url,omitempty"`
}

func NewServer(cfg config.Config, st *store.Store) (*Server, error) {
	tmpl, err := template.New("templates").Funcs(template.FuncMap{
		"withBase":    withBase,
		"join":        strings.Join,
		"statusClass": statusClass,
		"stars":       renderStars,
	}).ParseFS(assets, "templates/*.html")
	if err != nil {
		return nil, fmt.Errorf("parse templates: %w", err)
	}

	staticFS, err := fs.Sub(assets, "static")
	if err != nil {
		return nil, fmt.Errorf("load static assets: %w", err)
	}

	httpClient := &http.Client{
		Timeout: cfg.RequestTimeout,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}

	return &Server{
		cfg:     cfg,
		store:   st,
		catalog: catalog.NewService(st, httpClient),
		router:  router.NewClient(cfg.RouterURL, cfg.RequestTimeout),
		http:    httpClient,
		tmpl:    tmpl,
		static:  http.StripPrefix("/static/", http.FileServer(http.FS(staticFS))),
	}, nil
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/healthz":
		s.handleHealth(w, r)
		return
	case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/static/"):
		s.static.ServeHTTP(w, r)
		return
	case r.Method == http.MethodGet && r.URL.Path == "/setup":
		s.handleSetupPage(w, r)
		return
	case r.Method == http.MethodPost && r.URL.Path == "/setup":
		s.handleSetupSubmit(w, r)
		return
	case r.Method == http.MethodGet && r.URL.Path == "/":
		s.handleIndex(w, r)
		return
	case r.Method == http.MethodGet && r.URL.Path == "/sources":
		s.handleSourcesPage(w, r, "", "")
		return
	case r.Method == http.MethodPost && r.URL.Path == "/sources":
		s.handleSourceCreate(w, r)
		return
	case r.Method == http.MethodPost && strings.HasPrefix(r.URL.Path, "/sources/"):
		s.handleSourceAction(w, r)
		return
	case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/apps/"):
		s.handleAppDetail(w, r)
		return
	case r.Method == http.MethodPost && r.URL.Path == "/publish":
		s.handlePublish(w, r)
		return
	case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/publishes/"):
		s.handlePublishRoutes(w, r)
		return
	default:
		http.NotFound(w, r)
	}
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = io.WriteString(w, `{"status":"ok"}`)
}

func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	setupMode, err := s.store.GetSetting(ctx, "setup_complete")
	if err != nil {
		log.Printf("failed to check setup_complete setting: %v", err)
	}
	if setupMode == "" {
		s.redirectTo(w, r, "/setup")
		return
	}

	failedSyncs := s.syncEnabledSources(ctx)

	query := strings.TrimSpace(r.URL.Query().Get("q"))
	sourceFilter := strings.TrimSpace(r.URL.Query().Get("source"))
	tagFilter := strings.TrimSpace(r.URL.Query().Get("tag"))

	apps, err := s.store.ListCatalogApps(ctx, store.AppListFilter{
		Query:    query,
		SourceID: sourceFilter,
		Tag:      tagFilter,
	})
	if err != nil {
		s.renderError(w, http.StatusInternalServerError, "failed to query catalog apps", err)
		return
	}

	sources, err := s.store.ListSources(ctx)
	if err != nil {
		s.renderError(w, http.StatusInternalServerError, "failed to query sources", err)
		return
	}

	s.render(w, http.StatusOK, "index.html", indexPageData{
		BasePath:        s.basePathForRequest(r),
		Query:           query,
		SourceFilter:    sourceFilter,
		TagFilter:       tagFilter,
		Sources:         sources,
		Apps:            apps,
		CanDeploy:       s.canDeployDirectly(ctx),
		RouterBaseURL:   s.routerBaseURL(r),
		FailedSyncNames: failedSyncs,
	})
}

func (s *Server) handleSetupPage(w http.ResponseWriter, r *http.Request) {
	mode, _ := s.store.GetSetting(r.Context(), "setup_complete")
	s.render(w, http.StatusOK, "setup.html", setupPageData{
		BasePath:    s.basePathForRequest(r),
		CurrentMode: mode,
	})
}

func (s *Server) handleSetupSubmit(w http.ResponseWriter, r *http.Request) {
	currentMode, _ := s.store.GetSetting(r.Context(), "setup_complete")
	if err := r.ParseForm(); err != nil {
		s.render(w, http.StatusBadRequest, "setup.html", setupPageData{
			BasePath:    s.basePathForRequest(r),
			CurrentMode: currentMode,
			Error:       "Invalid form submission.",
		})
		return
	}

	action := strings.TrimSpace(r.Form.Get("action"))

	switch action {
	case "deploy":
		_, err := s.resolveRouterToken(r.Context())
		if err != nil {
			data := setupPageData{
				BasePath:    s.basePathForRequest(r),
				CurrentMode: currentMode,
				Error:       err.Error(),
			}
			if errors.Is(err, ErrSecretsAppMissing) {
				data.SecretsMissing = true
				// Prefill the Deploy App form with the bundled Secrets app's
				// file:// URL so the user just has to click "Authorize & Deploy".
				// This is the ansible-provisioned default (apps_dir_override).
				secretsRepo := "file:///home/host/openhost/apps/secrets"
				data.SecretsInstallURL = s.routerBaseURL(r) + "/add_app?repo=" + url.QueryEscape(secretsRepo)
			}
			s.render(w, http.StatusOK, "setup.html", data)
			return
		}
		if err := s.store.SetSetting(r.Context(), "setup_complete", "deploy"); err != nil {
			s.render(w, http.StatusInternalServerError, "setup.html", setupPageData{
				BasePath:    s.basePathForRequest(r),
				CurrentMode: currentMode,
				Error:       "Failed to save setup preference: " + err.Error(),
			})
			return
		}
		s.redirectTo(w, r, "/")

	case "skip":
		if err := s.store.SetSetting(r.Context(), "setup_complete", "manual"); err != nil {
			s.render(w, http.StatusInternalServerError, "setup.html", setupPageData{
				BasePath:    s.basePathForRequest(r),
				CurrentMode: currentMode,
				Error:       "Failed to save setup preference: " + err.Error(),
			})
			return
		}
		s.redirectTo(w, r, "/")

	default:
		s.render(w, http.StatusBadRequest, "setup.html", setupPageData{
			BasePath:    s.basePathForRequest(r),
			CurrentMode: currentMode,
			Error:       "Unknown action.",
		})
	}
}

func (s *Server) handleSourcesPage(w http.ResponseWriter, r *http.Request, message string, errMsg string) {
	sources, err := s.store.ListSources(r.Context())
	if err != nil {
		s.renderError(w, http.StatusInternalServerError, "failed to query sources", err)
		return
	}

	s.render(w, http.StatusOK, "sources.html", sourcesPageData{
		BasePath: s.basePathForRequest(r),
		Sources:  sources,
		Message:  message,
		Error:    errMsg,
	})
}

func (s *Server) handleSourceCreate(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseForm(); err != nil {
		s.handleSourcesPage(w, r, "", "invalid form submission")
		return
	}

	name := strings.TrimSpace(r.Form.Get("name"))
	sourceURL := strings.TrimSpace(r.Form.Get("url"))

	if sourceURL == "" {
		s.handleSourcesPage(w, r, "", "source URL is required")
		return
	}
	parsedSourceURL, err := url.ParseRequestURI(sourceURL)
	if err != nil {
		s.handleSourcesPage(w, r, "", "source URL must be a valid absolute URL")
		return
	}
	if parsedSourceURL.Scheme != "https" && parsedSourceURL.Scheme != "http" {
		s.handleSourcesPage(w, r, "", "source URL must use http or https")
		return
	}

	// Auto-generate internal source ID from the name (or URL as fallback).
	// The URL is the user-visible unique identifier; the ID is internal only.
	sourceID := makeSlug(name)
	if sourceID == "" {
		sourceID = makeSlug(sourceURL)
	}
	if sourceID == "" {
		s.handleSourcesPage(w, r, "", "could not derive a source id from the provided URL")
		return
	}

	if name == "" {
		name = sourceID
	}

	err = s.store.CreateSource(r.Context(), store.Source{
		ID:      sourceID,
		Name:    name,
		URL:     sourceURL,
		Enabled: true,
	})
	if err != nil {
		s.handleSourcesPage(w, r, "", "failed to add source: "+humanizeErr(err))
		return
	}

	if err := s.catalog.SyncSource(r.Context(), sourceID); err != nil {
		s.handleSourcesPage(w, r, "", "source added, but initial sync failed: "+humanizeErr(err))
		return
	}

	s.redirectTo(w, r, "/sources")
}

func (s *Server) handleSourceAction(w http.ResponseWriter, r *http.Request) {
	rest := strings.TrimPrefix(r.URL.Path, "/sources/")
	parts := strings.Split(rest, "/")
	if len(parts) != 2 {
		http.NotFound(w, r)
		return
	}

	sourceID := parts[0]
	action := parts[1]

	ctx := r.Context()
	switch action {
	case "toggle":
		src, err := s.store.GetSource(ctx, sourceID)
		if err != nil {
			s.handleSourcesPage(w, r, "", "failed to load source")
			return
		}
		if err := s.store.SetSourceEnabled(ctx, sourceID, !src.Enabled); err != nil {
			s.handleSourcesPage(w, r, "", "failed to update source")
			return
		}
		s.redirectTo(w, r, "/sources")
	case "delete":
		if err := s.store.DeleteSource(ctx, sourceID); err != nil {
			s.handleSourcesPage(w, r, "", "failed to delete source")
			return
		}
		s.redirectTo(w, r, "/sources")
	default:
		http.NotFound(w, r)
	}
}

func (s *Server) handleAppDetail(w http.ResponseWriter, r *http.Request) {
	rest := strings.TrimPrefix(r.URL.Path, "/apps/")
	parts := strings.Split(rest, "/")
	if len(parts) != 2 {
		http.NotFound(w, r)
		return
	}

	app, err := s.store.GetCatalogApp(r.Context(), parts[0], parts[1])
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.NotFound(w, r)
			return
		}
		s.renderError(w, http.StatusInternalServerError, "failed to load app", err)
		return
	}

	s.render(w, http.StatusOK, "app.html", appPageData{
		BasePath:      s.basePathForRequest(r),
		App:           app,
		CanDeploy:     s.canDeployDirectly(r.Context()),
		RouterBaseURL: s.routerBaseURL(r),
	})
}

func (s *Server) handlePublish(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseForm(); err != nil {
		s.renderError(w, http.StatusBadRequest, "invalid form submission", err)
		return
	}

	sourceID := strings.TrimSpace(r.Form.Get("source_id"))
	appID := strings.TrimSpace(r.Form.Get("app_id"))
	requestedName := strings.TrimSpace(r.Form.Get("app_name"))

	if sourceID == "" || appID == "" {
		s.renderError(w, http.StatusBadRequest, "source_id and app_id are required", nil)
		return
	}

	app, err := s.store.GetCatalogApp(r.Context(), sourceID, appID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.NotFound(w, r)
			return
		}
		s.renderError(w, http.StatusInternalServerError, "failed to load catalog app", err)
		return
	}

	if requestedName == "" {
		requestedName = app.AppID
	}
	if !appNamePattern.MatchString(requestedName) {
		s.render(w, http.StatusBadRequest, "app.html", appPageData{
			BasePath: s.basePathForRequest(r),
			App:      app,
			Error:    "app name must be lowercase alphanumeric with optional interior hyphens",
		})
		return
	}

	if !s.repoAllowed(app.RepoURL) {
		s.render(w, http.StatusBadRequest, "app.html", appPageData{
			BasePath: s.basePathForRequest(r),
			App:      app,
			Error:    "repo URL scheme is not allowed by this catalog configuration",
		})
		return
	}

	repoForDeploy := app.RepoURL
	if app.RepoRef != "" {
		repoForDeploy = repoForDeploy + "@" + app.RepoRef
	}

	publish := store.Publish{
		ID:               newPublishID(),
		SourceID:         app.SourceID,
		AppID:            app.AppID,
		Title:            app.Title,
		RequestedAppName: requestedName,
		RepoURL:          app.RepoURL,
		RepoRef:          app.RepoRef,
		RouterAppName:    requestedName,
		Status:           "building",
	}

	token, tokenErr := s.resolveRouterToken(r.Context())
	if tokenErr != nil {
		publish.Status = "redirect_required"
		publish.ErrorMessage = tokenErr.Error()
		publish.ManualInstallURL = s.manualInstallURL(r, repoForDeploy)
		if err := s.store.CreatePublish(r.Context(), publish); err != nil {
			s.renderError(w, http.StatusInternalServerError, "failed to create publish record", err)
			return
		}
		s.redirectTo(w, r, "/publishes/"+publish.ID)
		return
	}

	deployResult, err := s.router.Deploy(r.Context(), token, repoForDeploy, requestedName)
	if err != nil {
		publish.Status = "error"
		publish.ErrorMessage = err.Error()
		publish.ManualInstallURL = s.manualInstallURL(r, repoForDeploy)
		if err := s.store.CreatePublish(r.Context(), publish); err != nil {
			s.renderError(w, http.StatusInternalServerError, "failed to save publish result", err)
			return
		}
		s.redirectTo(w, r, "/publishes/"+publish.ID)
		return
	}

	if deployResult.AppName != "" {
		publish.RouterAppName = deployResult.AppName
	}
	if deployResult.Status != "" {
		publish.Status = deployResult.Status
	}

	if err := s.store.CreatePublish(r.Context(), publish); err != nil {
		s.renderError(w, http.StatusInternalServerError, "failed to create publish record", err)
		return
	}

	s.redirectTo(w, r, "/publishes/"+publish.ID)
}

func (s *Server) handlePublishRoutes(w http.ResponseWriter, r *http.Request) {
	rest := strings.TrimPrefix(r.URL.Path, "/publishes/")
	parts := strings.Split(rest, "/")
	if len(parts) == 0 || parts[0] == "" {
		http.NotFound(w, r)
		return
	}

	publishID := parts[0]

	if len(parts) == 2 && parts[1] == "status.json" {
		s.handlePublishStatusJSON(w, r, publishID)
		return
	}
	if len(parts) == 2 && parts[1] == "logs.txt" {
		s.handlePublishLogs(w, r, publishID)
		return
	}
	if len(parts) == 1 {
		s.handlePublishPage(w, r, publishID)
		return
	}

	http.NotFound(w, r)
}

func (s *Server) handlePublishPage(w http.ResponseWriter, r *http.Request, publishID string) {
	publish, err := s.store.GetPublish(r.Context(), publishID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.NotFound(w, r)
			return
		}
		s.renderError(w, http.StatusInternalServerError, "failed to load publish", err)
		return
	}

	publish = s.refreshPublishState(r.Context(), publish)

	s.render(w, http.StatusOK, "publish.html", publishPageData{
		BasePath:     s.basePathForRequest(r),
		Publish:      publish,
		Terminal:     isTerminalPublishStatus(publish.Status),
		RouterAppURL: s.appExternalURL(r, publish.RouterAppName),
		RouterPage:   s.routerPageURL(r, publish.RouterAppName),
	})
}

func (s *Server) handlePublishStatusJSON(w http.ResponseWriter, r *http.Request, publishID string) {
	publish, err := s.store.GetPublish(r.Context(), publishID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.NotFound(w, r)
			return
		}
		s.renderError(w, http.StatusInternalServerError, "failed to load publish", err)
		return
	}

	publish = s.refreshPublishState(r.Context(), publish)

	resp := publishStatusResponse{
		ID:               publish.ID,
		Status:           publish.Status,
		ErrorMessage:     publish.ErrorMessage,
		RouterAppName:    publish.RouterAppName,
		ManualInstallURL: publish.ManualInstallURL,
		Terminal:         isTerminalPublishStatus(publish.Status),
	}
	if publish.RouterAppName != "" {
		resp.RouterAppURL = s.appExternalURL(r, publish.RouterAppName)
		resp.RouterPageURL = s.routerPageURL(r, publish.RouterAppName)
	}

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(resp); err != nil {
		log.Printf("failed to encode publish status response: %v", err)
	}
}

func (s *Server) handlePublishLogs(w http.ResponseWriter, r *http.Request, publishID string) {
	publish, err := s.store.GetPublish(r.Context(), publishID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			http.NotFound(w, r)
			return
		}
		s.renderError(w, http.StatusInternalServerError, "failed to load publish", err)
		return
	}

	if publish.RouterAppName == "" {
		http.Error(w, "no app name available for logs", http.StatusBadRequest)
		return
	}

	token, err := s.resolveRouterToken(r.Context())
	if err != nil {
		http.Error(w, err.Error(), http.StatusServiceUnavailable)
		return
	}

	logsText, err := s.router.AppLogs(r.Context(), token, publish.RouterAppName)
	if err != nil {
		http.Error(w, "failed to load logs: "+err.Error(), http.StatusBadGateway)
		return
	}

	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	_, _ = io.WriteString(w, logsText)
}

func (s *Server) refreshPublishState(ctx context.Context, publish store.Publish) store.Publish {
	if isTerminalPublishStatus(publish.Status) {
		return publish
	}
	if publish.RouterAppName == "" {
		return publish
	}

	token, err := s.resolveRouterToken(ctx)
	if err != nil {
		return publish
	}

	status, err := s.router.AppStatus(ctx, token, publish.RouterAppName)
	if err != nil {
		return publish
	}

	if status.Status == "" {
		return publish
	}

	publish.Status = status.Status
	if status.Error != "" {
		publish.ErrorMessage = status.Error
	} else if status.Status != "error" {
		publish.ErrorMessage = ""
	}

	if err := s.store.UpdatePublish(ctx, publish); err != nil {
		log.Printf("failed to update publish state for %s: %v", publish.ID, err)
	}

	return publish
}

// canDeployDirectly returns true if the catalog has (or can acquire) a
// router token for one-click deploys. Used to decide whether to show
// "Publish" vs "Install" buttons on page loads.
//
// Previously this only looked at the in-memory token cache populated
// lazily by publish-time code, so after a container restart (or >30s
// of idle) the cache was cold and the UI silently fell back to the
// manual-install path even when the secrets app still had the token.
// We now actually try to resolve the token via resolveRouterToken,
// which uses the same cache but lazy-fetches from the secrets app on
// miss. Token fetches from the bundled secrets app are cheap (a local
// HTTP call inside the compute space), so doing this on every page
// render is fine.
func (s *Server) canDeployDirectly(ctx context.Context) bool {
	mode, err := s.store.GetSetting(ctx, "setup_complete")
	if err != nil {
		log.Printf("canDeployDirectly: failed to read setup_complete: %v", err)
		return false
	}
	if mode == "manual" || mode == "" {
		return false
	}

	if _, err := s.resolveRouterToken(ctx); err != nil {
		log.Printf("canDeployDirectly: no router token available: %v", err)
		return false
	}
	return true
}

func (s *Server) resolveRouterToken(ctx context.Context) (string, error) {
	if strings.TrimSpace(s.cfg.RouterToken) != "" {
		return s.cfg.RouterToken, nil
	}

	s.tokenMu.Lock()
	if s.tokenVal != "" && time.Since(s.tokenTS) < 30*time.Second {
		v := s.tokenVal
		s.tokenMu.Unlock()
		return v, nil
	}
	s.tokenMu.Unlock()

	if s.cfg.AppToken == "" {
		return "", errors.New("one-click deploy unavailable: no APP_REPO_ROUTER_TOKEN configured and no OPENHOST_APP_TOKEN available")
	}

	token, err := s.fetchRouterTokenFromSecrets(ctx)
	if err != nil {
		return "", err
	}

	s.tokenMu.Lock()
	s.tokenVal = token
	s.tokenTS = time.Now()
	s.tokenMu.Unlock()

	return token, nil
}

func (s *Server) fetchRouterTokenFromSecrets(ctx context.Context) (string, error) {
	endpoint := s.cfg.RouterURL + "/_services/secrets/get"
	body := strings.NewReader(`{"keys":["APP_REPO_ROUTER_TOKEN"]}`)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, body)
	if err != nil {
		return "", fmt.Errorf("create secrets request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+s.cfg.AppToken)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	resp, err := s.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("request secrets service: %w", err)
	}
	defer resp.Body.Close()

	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))

	if resp.StatusCode == http.StatusForbidden {
		approveURL := s.requestSecretsPermission(ctx)
		if approveURL != "" {
			return "", fmt.Errorf("permission needed to read APP_REPO_ROUTER_TOKEN. Approve at %s", approveURL)
		}
		return "", errors.New("permission denied when requesting APP_REPO_ROUTER_TOKEN from secrets service")
	}
	if resp.StatusCode == http.StatusServiceUnavailable {
		// Parse the JSON error to check for "service not available" (secrets app not installed)
		var svcErr struct {
			Error   string `json:"error"`
			Message string `json:"message"`
		}
		if json.Unmarshal(raw, &svcErr) == nil && svcErr.Error == "service_not_available" {
			return "", ErrSecretsAppMissing
		}
		return "", fmt.Errorf("secrets service is unavailable (HTTP %d)", resp.StatusCode)
	}
	if resp.StatusCode >= 400 {
		var jsonErr struct {
			Message string `json:"message"`
		}
		if json.Unmarshal(raw, &jsonErr) == nil && jsonErr.Message != "" {
			return "", fmt.Errorf("secrets service error: %s", jsonErr.Message)
		}
		return "", fmt.Errorf("secrets service error (HTTP %d)", resp.StatusCode)
	}

	var payload struct {
		Secrets map[string]string `json:"secrets"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return "", fmt.Errorf("decode secrets response: %w", err)
	}

	token := strings.TrimSpace(payload.Secrets["APP_REPO_ROUTER_TOKEN"])
	if token == "" {
		approveURL := s.requestSecretsPermission(ctx)
		if approveURL != "" {
			return "", fmt.Errorf("APP_REPO_ROUTER_TOKEN not found. Add it in secrets and approve key access at %s", approveURL)
		}
		return "", errors.New("APP_REPO_ROUTER_TOKEN not found in secrets")
	}

	return token, nil
}

func (s *Server) requestSecretsPermission(ctx context.Context) string {
	endpoint := s.cfg.RouterURL + "/_services/request-permission"
	body := strings.NewReader(`{"service":"secrets","key":"APP_REPO_ROUTER_TOKEN","reason":"Deploy apps via openhost-catalog"}`)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, body)
	if err != nil {
		return ""
	}
	req.Header.Set("Authorization", "Bearer "+s.cfg.AppToken)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	resp, err := s.http.Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return ""
	}

	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 32*1024))
	var payload struct {
		Status     string `json:"status"`
		ApproveURL string `json:"approve_url"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return ""
	}
	return strings.TrimSpace(payload.ApproveURL)
}

// syncEnabledSources syncs all enabled sources inline. Called on every catalog
// page load so users always see fresh data. Returns the names of sources whose
// sync failed so the caller can surface a stale-data warning in the UI.
func (s *Server) syncEnabledSources(ctx context.Context) []string {
	syncCtx, cancel := context.WithTimeout(ctx, sourceSyncBudget)
	defer cancel()

	sources, err := s.store.ListSources(syncCtx)
	if err != nil {
		log.Printf("sync: failed to list sources: %v", err)
		return nil
	}
	var failed []string
	for _, src := range sources {
		if !src.Enabled {
			continue
		}
		if err := s.catalog.SyncSource(syncCtx, src.ID); err != nil {
			log.Printf("sync: failed to sync source %s: %v", src.ID, err)
			failed = append(failed, src.Name)
		}
	}
	return failed
}

func (s *Server) repoAllowed(repoURL string) bool {
	u, err := url.Parse(repoURL)
	if err != nil {
		return false
	}
	scheme := strings.ToLower(u.Scheme)
	switch scheme {
	case "https":
		return true
	case "http":
		return s.cfg.AllowHTTPRepoURLs
	case "file":
		return s.cfg.AllowFileRepoURLs
	default:
		return false
	}
}

func (s *Server) appExternalURL(r *http.Request, appName string) string {
	if appName == "" {
		return ""
	}
	host := s.routerHost(r)
	proto := forwardedProto(r)
	if host == "" {
		return ""
	}
	return proto + "://" + appName + "." + host + "/"
}

func (s *Server) routerPageURL(r *http.Request, appName string) string {
	if appName == "" {
		return ""
	}
	return s.routerBaseURL(r) + "/app_detail/" + url.PathEscape(appName)
}

func (s *Server) manualInstallURL(r *http.Request, repoURL string) string {
	return s.routerBaseURL(r) + "/add_app?" + url.Values{"repo": {repoURL}}.Encode()
}

func (s *Server) routerBaseURL(r *http.Request) string {
	return forwardedProto(r) + "://" + s.routerHost(r)
}

func (s *Server) routerHost(r *http.Request) string {
	hostPort := strings.TrimSpace(r.Header.Get("X-Forwarded-Host"))
	if hostPort == "" {
		hostPort = r.Host
	}

	host, port := splitHostPort(hostPort)
	if strings.HasPrefix(host, s.cfg.AppName+".") {
		host = strings.TrimPrefix(host, s.cfg.AppName+".")
	}
	if port == "" {
		return host
	}
	return host + ":" + port
}

func (s *Server) basePathForRequest(r *http.Request) string {
	basePath := strings.TrimSpace(s.cfg.AppBasePath)
	if basePath == "" || basePath == "/" {
		return ""
	}

	host, _ := splitHostPort(strings.TrimSpace(r.Header.Get("X-Forwarded-Host")))
	if host == "" {
		host, _ = splitHostPort(r.Host)
	}

	if strings.HasPrefix(host, s.cfg.AppName+".") {
		return ""
	}

	return basePath
}

func (s *Server) redirectTo(w http.ResponseWriter, r *http.Request, path string) {
	http.Redirect(w, r, withBase(s.basePathForRequest(r), path), http.StatusSeeOther)
}

func (s *Server) render(w http.ResponseWriter, status int, name string, data any) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	// Never cache catalog pages - they are synced on every load and stale
	// views would make source edits look like they haven't taken effect.
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	if err := s.tmpl.ExecuteTemplate(w, name, data); err != nil {
		log.Printf("template render error for %s: %v", name, err)
	}
}

func (s *Server) renderError(w http.ResponseWriter, status int, message string, err error) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(status)
	if err != nil {
		_, _ = io.WriteString(w, message+": "+humanizeErr(err))
		return
	}
	_, _ = io.WriteString(w, message)
}

// renderStars returns a 5-character string combining filled and
// hollow stars. level is clamped into [0, 5]; level 0 renders as
// all-hollow so the table column width is stable for unrated apps.
func renderStars(level int) string {
	if level < 0 {
		level = 0
	}
	if level > 5 {
		level = 5
	}
	return strings.Repeat("\u2605", level) + strings.Repeat("\u2606", 5-level)
}

func statusClass(status string) string {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "running":
		return "status-running"
	case "error":
		return "status-error"
	case "building", "starting":
		return "status-active"
	case "redirect_required":
		return "status-warn"
	default:
		return "status-neutral"
	}
}

func isTerminalPublishStatus(status string) bool {
	s := strings.ToLower(strings.TrimSpace(status))
	switch s {
	case "running", "error", "stopped", "redirect_required":
		return true
	default:
		return false
	}
}

func withBase(basePath string, path string) string {
	basePath = strings.TrimSpace(basePath)
	if basePath == "" || basePath == "/" {
		if path == "" {
			return "/"
		}
		if strings.HasPrefix(path, "/") {
			return path
		}
		return "/" + path
	}

	if !strings.HasPrefix(basePath, "/") {
		basePath = "/" + basePath
	}
	basePath = strings.TrimRight(basePath, "/")

	if path == "" || path == "/" {
		return basePath + "/"
	}
	if strings.HasPrefix(path, "/") {
		return basePath + path
	}
	return basePath + "/" + path
}

func splitHostPort(hostPort string) (string, string) {
	hostPort = strings.TrimSpace(hostPort)
	if hostPort == "" {
		return "", ""
	}
	host, port, err := net.SplitHostPort(hostPort)
	if err == nil {
		return host, port
	}
	if strings.Contains(err.Error(), "missing port in address") {
		return hostPort, ""
	}
	return hostPort, ""
}

func forwardedProto(r *http.Request) string {
	proto := strings.TrimSpace(r.Header.Get("X-Forwarded-Proto"))
	if proto != "" {
		return proto
	}
	if r.TLS != nil {
		return "https"
	}
	return "http"
}

func newPublishID() string {
	b := make([]byte, 12)
	if _, err := rand.Read(b); err != nil {
		panic("failed to generate publish id")
	}
	return hex.EncodeToString(b)
}

func makeSlug(in string) string {
	in = strings.ToLower(strings.TrimSpace(in))
	replacer := strings.NewReplacer(
		" ", "-",
		"_", "-",
		"/", "-",
		".", "-",
		":", "-",
	)
	in = replacer.Replace(in)
	in = regexp.MustCompile(`[^a-z0-9-]+`).ReplaceAllString(in, "")
	in = regexp.MustCompile(`-+`).ReplaceAllString(in, "-")
	in = strings.Trim(in, "-")
	return in
}

func humanizeErr(err error) string {
	msg := strings.TrimSpace(err.Error())
	msg = strings.TrimPrefix(msg, "sqlite: ")
	msg = strings.TrimPrefix(msg, "constraint failed: ")
	return msg
}
