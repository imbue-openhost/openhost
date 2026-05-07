package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

type Store struct {
	db *sql.DB
}

type Source struct {
	ID         string
	Name       string
	URL        string
	Enabled    bool
	LastSyncAt string
	LastError  string
	CreatedAt  string
	UpdatedAt  string
}

type CatalogApp struct {
	SourceID                 string
	SourceName               string
	AppID                    string
	Title                    string
	Description              string
	RepoURL                  string
	RepoRef                  string
	IconURL                  string
	Tags                     []string
	Categories               []string
	WebsiteURL               string
	DocsURL                  string
	OpenhostIntegrationScore int // 1-5 when supplied, 0 means unrated
	UpdatedAt                string
}

type Publish struct {
	ID               string
	SourceID         string
	AppID            string
	Title            string
	RequestedAppName string
	RepoURL          string
	RepoRef          string
	RouterAppName    string
	Status           string
	ErrorMessage     string
	ManualInstallURL string
	CreatedAt        string
	UpdatedAt        string
}

type AppListFilter struct {
	Query    string
	SourceID string
	Tag      string
}

func Open(path string) (*Store, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil && !errors.Is(err, os.ErrExist) {
		return nil, fmt.Errorf("create db parent directory: %w", err)
	}

	fileHandle, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open db file for read/write: %w", err)
	}
	_ = fileHandle.Close()

	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("open sqlite db: %w", err)
	}

	if _, err := db.Exec("SELECT 1"); err != nil {
		db.Close()
		return nil, fmt.Errorf("open sqlite connection: %w", err)
	}

	if _, err := db.Exec("PRAGMA foreign_keys=ON"); err != nil {
		db.Close()
		return nil, fmt.Errorf("enable foreign keys: %w", err)
	}
	if _, err := db.Exec("PRAGMA busy_timeout=5000"); err != nil {
		db.Close()
		return nil, fmt.Errorf("set sqlite busy timeout: %w", err)
	}

	if _, err := db.Exec("PRAGMA journal_mode=WAL"); err != nil {
		log.Printf("store: WAL unavailable, falling back to DELETE mode: %v", err)
		if _, errDelete := db.Exec("PRAGMA journal_mode=DELETE"); errDelete != nil {
			db.Close()
			return nil, fmt.Errorf("configure journal mode failed (wal: %v, delete fallback: %w)", err, errDelete)
		}
	}

	return &Store{db: db}, nil
}

func (s *Store) Close() error {
	return s.db.Close()
}

func (s *Store) Init(ctx context.Context) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS sources (
			id TEXT PRIMARY KEY,
			name TEXT NOT NULL,
			url TEXT NOT NULL UNIQUE,
			enabled INTEGER NOT NULL DEFAULT 1,
			last_sync_at TEXT NOT NULL DEFAULT '',
			last_error TEXT NOT NULL DEFAULT '',
			created_at TEXT NOT NULL,
			updated_at TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS catalog_apps (
			source_id TEXT NOT NULL,
			app_id TEXT NOT NULL,
			title TEXT NOT NULL,
			description TEXT NOT NULL DEFAULT '',
			repo_url TEXT NOT NULL,
			repo_ref TEXT NOT NULL DEFAULT '',
			icon_url TEXT NOT NULL DEFAULT '',
			tags_json TEXT NOT NULL DEFAULT '[]',
			categories_json TEXT NOT NULL DEFAULT '[]',
			website_url TEXT NOT NULL DEFAULT '',
			docs_url TEXT NOT NULL DEFAULT '',
			updated_at TEXT NOT NULL,
			PRIMARY KEY (source_id, app_id),
			FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
		)`,
		`CREATE TABLE IF NOT EXISTS publishes (
			id TEXT PRIMARY KEY,
			source_id TEXT NOT NULL,
			app_id TEXT NOT NULL,
			title TEXT NOT NULL,
			requested_app_name TEXT NOT NULL,
			repo_url TEXT NOT NULL,
			repo_ref TEXT NOT NULL DEFAULT '',
			router_app_name TEXT NOT NULL DEFAULT '',
			status TEXT NOT NULL,
			error_message TEXT NOT NULL DEFAULT '',
			manual_install_url TEXT NOT NULL DEFAULT '',
			created_at TEXT NOT NULL,
			updated_at TEXT NOT NULL,
			FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
		)`,
		`CREATE TABLE IF NOT EXISTS settings (
			key TEXT PRIMARY KEY,
			value TEXT NOT NULL
		)`,
		`CREATE INDEX IF NOT EXISTS idx_catalog_apps_title ON catalog_apps(title)`,
		`CREATE INDEX IF NOT EXISTS idx_publishes_created_at ON publishes(created_at DESC)`,
	}

	for _, stmt := range statements {
		if _, err := s.db.ExecContext(ctx, stmt); err != nil {
			return fmt.Errorf("initialize schema: %w", err)
		}
	}

	// Schema evolution for the integration rating feature.
	// Both statement sets are idempotent: ADD COLUMN raises "duplicate
	// column name" when the column exists, DROP COLUMN raises "no such
	// column" when it does not. We swallow each expected-noop error.

	addColumns := []string{
		`ALTER TABLE catalog_apps ADD COLUMN openhost_integration_score INTEGER NOT NULL DEFAULT 0`,
	}
	for _, stmt := range addColumns {
		if _, err := s.db.ExecContext(ctx, stmt); err != nil {
			if !strings.Contains(err.Error(), "duplicate column name") {
				return fmt.Errorf("add integration score column: %w", err)
			}
		}
	}

	// Drop the earlier has/missing/vocab columns introduced by a prior
	// schema version. Data in them is fully regenerable from the feed
	// so losing it is safe.
	dropColumns := []string{
		`ALTER TABLE catalog_apps DROP COLUMN integration_json`,
		`ALTER TABLE sources DROP COLUMN integrations_vocab_json`,
	}
	for _, stmt := range dropColumns {
		if _, err := s.db.ExecContext(ctx, stmt); err != nil {
			if !strings.Contains(err.Error(), "no such column") {
				return fmt.Errorf("drop legacy integration columns: %w", err)
			}
		}
	}
	return nil
}

func (s *Store) CreateSource(ctx context.Context, src Source) error {
	now := nowString()
	_, err := s.db.ExecContext(
		ctx,
		`INSERT INTO sources
		(id, name, url, enabled, last_sync_at, last_error, created_at, updated_at)
		VALUES (?, ?, ?, ?, '', '', ?, ?)`,
		src.ID,
		src.Name,
		src.URL,
		boolToInt(src.Enabled),
		now,
		now,
	)
	if err != nil {
		return fmt.Errorf("insert source: %w", err)
	}
	return nil
}

func (s *Store) GetSource(ctx context.Context, id string) (Source, error) {
	row := s.db.QueryRowContext(
		ctx,
		`SELECT id, name, url, enabled, last_sync_at, last_error, created_at, updated_at
		 FROM sources WHERE id = ?`,
		id,
	)
	return scanSource(row)
}

func (s *Store) ListSources(ctx context.Context) ([]Source, error) {
	rows, err := s.db.QueryContext(
		ctx,
		`SELECT id, name, url, enabled, last_sync_at, last_error, created_at, updated_at
		 FROM sources ORDER BY lower(name), id`,
	)
	if err != nil {
		return nil, fmt.Errorf("query sources: %w", err)
	}
	defer rows.Close()

	out := make([]Source, 0)
	for rows.Next() {
		s, err := scanSource(rows)
		if err != nil {
			return nil, fmt.Errorf("scan source row: %w", err)
		}
		out = append(out, s)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate sources: %w", err)
	}
	return out, nil
}

func (s *Store) SetSourceEnabled(ctx context.Context, id string, enabled bool) error {
	_, err := s.db.ExecContext(
		ctx,
		`UPDATE sources SET enabled = ?, updated_at = ? WHERE id = ?`,
		boolToInt(enabled),
		nowString(),
		id,
	)
	if err != nil {
		return fmt.Errorf("update source enabled: %w", err)
	}
	return nil
}

func (s *Store) DeleteSource(ctx context.Context, id string) error {
	_, err := s.db.ExecContext(ctx, `DELETE FROM sources WHERE id = ?`, id)
	if err != nil {
		return fmt.Errorf("delete source: %w", err)
	}
	return nil
}

func (s *Store) ReplaceCatalogAppsForSource(ctx context.Context, sourceID string, apps []CatalogApp) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin transaction: %w", err)
	}
	defer tx.Rollback()

	if _, err := tx.ExecContext(ctx, `DELETE FROM catalog_apps WHERE source_id = ?`, sourceID); err != nil {
		return fmt.Errorf("clear source catalog apps: %w", err)
	}

	insertStmt := `INSERT INTO catalog_apps
	(source_id, app_id, title, description, repo_url, repo_ref, icon_url,
	 tags_json, categories_json, website_url, docs_url,
	 openhost_integration_score, updated_at)
	VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`

	now := nowString()
	for _, app := range apps {
		tagsJSON, err := json.Marshal(app.Tags)
		if err != nil {
			return fmt.Errorf("marshal tags for %s/%s: %w", app.SourceID, app.AppID, err)
		}
		categoriesJSON, err := json.Marshal(app.Categories)
		if err != nil {
			return fmt.Errorf("marshal categories for %s/%s: %w", app.SourceID, app.AppID, err)
		}

		if _, err := tx.ExecContext(
			ctx,
			insertStmt,
			sourceID,
			app.AppID,
			app.Title,
			app.Description,
			app.RepoURL,
			app.RepoRef,
			app.IconURL,
			string(tagsJSON),
			string(categoriesJSON),
			app.WebsiteURL,
			app.DocsURL,
			app.OpenhostIntegrationScore,
			now,
		); err != nil {
			return fmt.Errorf("insert catalog app %s/%s: %w", sourceID, app.AppID, err)
		}
	}

	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit catalog apps transaction: %w", err)
	}
	return nil
}

// MarkSourceSynced records a successful sync: updates the source name,
// clears any prior error, and stamps last_sync_at.
func (s *Store) MarkSourceSynced(ctx context.Context, sourceID string, name string) error {
	now := nowString()
	_, err := s.db.ExecContext(
		ctx,
		`UPDATE sources
		 SET name = ?, last_sync_at = ?, last_error = '', updated_at = ?
		 WHERE id = ?`,
		name,
		now,
		now,
		sourceID,
	)
	if err != nil {
		return fmt.Errorf("mark source synced: %w", err)
	}
	return nil
}

// MarkSourceSyncFailed records a failed sync: stores the error but does NOT
// bump last_sync_at, so "Last Sync" remains the last successful sync time.
func (s *Store) MarkSourceSyncFailed(ctx context.Context, sourceID string, lastError string) error {
	_, err := s.db.ExecContext(
		ctx,
		`UPDATE sources SET last_error = ?, updated_at = ? WHERE id = ?`,
		lastError,
		nowString(),
		sourceID,
	)
	if err != nil {
		return fmt.Errorf("mark source sync failed: %w", err)
	}
	return nil
}

func (s *Store) ListCatalogApps(ctx context.Context, filter AppListFilter) ([]CatalogApp, error) {
	query := `SELECT
		ca.source_id,
		s.name,
		ca.app_id,
		ca.title,
		ca.description,
		ca.repo_url,
		ca.repo_ref,
		ca.icon_url,
		ca.tags_json,
		ca.categories_json,
		ca.website_url,
		ca.docs_url,
		ca.openhost_integration_score,
		ca.updated_at
	FROM catalog_apps ca
	JOIN sources s ON s.id = ca.source_id
	WHERE s.enabled = 1`

	args := make([]any, 0)

	if filter.SourceID != "" {
		query += ` AND ca.source_id = ?`
		args = append(args, filter.SourceID)
	}
	if filter.Query != "" {
		q := strings.ToLower(filter.Query)
		query += ` AND (
			lower(ca.app_id) LIKE ? OR
			lower(ca.title) LIKE ? OR
			lower(ca.description) LIKE ?
		)`
		like := "%" + q + "%"
		args = append(args, like, like, like)
	}
	if filter.Tag != "" {
		query += ` AND ca.tags_json LIKE ?`
		args = append(args, "%\""+filter.Tag+"\"%")
	}

	// Higher-integrated apps first; ties broken alphabetically by
	// title. Unrated apps (score = 0) land at the bottom.
	query += ` ORDER BY ca.openhost_integration_score DESC, lower(ca.title), ca.app_id`

	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("query catalog apps: %w", err)
	}
	defer rows.Close()

	out := make([]CatalogApp, 0)
	for rows.Next() {
		var app CatalogApp
		var tagsJSON, categoriesJSON string
		if err := rows.Scan(
			&app.SourceID,
			&app.SourceName,
			&app.AppID,
			&app.Title,
			&app.Description,
			&app.RepoURL,
			&app.RepoRef,
			&app.IconURL,
			&tagsJSON,
			&categoriesJSON,
			&app.WebsiteURL,
			&app.DocsURL,
			&app.OpenhostIntegrationScore,
			&app.UpdatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan catalog app row: %w", err)
		}
		app.Tags = decodeJSONList(tagsJSON)
		app.Categories = decodeJSONList(categoriesJSON)
		out = append(out, app)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate catalog apps: %w", err)
	}

	return out, nil
}

func (s *Store) GetCatalogApp(ctx context.Context, sourceID, appID string) (CatalogApp, error) {
	row := s.db.QueryRowContext(
		ctx,
		`SELECT
			ca.source_id,
			s.name,
			ca.app_id,
			ca.title,
			ca.description,
			ca.repo_url,
			ca.repo_ref,
			ca.icon_url,
			ca.tags_json,
			ca.categories_json,
			ca.website_url,
			ca.docs_url,
			ca.openhost_integration_score,
			ca.updated_at
		 FROM catalog_apps ca
		 JOIN sources s ON s.id = ca.source_id
		 WHERE ca.source_id = ? AND ca.app_id = ?`,
		sourceID,
		appID,
	)

	var app CatalogApp
	var tagsJSON, categoriesJSON string
	if err := row.Scan(
		&app.SourceID,
		&app.SourceName,
		&app.AppID,
		&app.Title,
		&app.Description,
		&app.RepoURL,
		&app.RepoRef,
		&app.IconURL,
		&tagsJSON,
		&categoriesJSON,
		&app.WebsiteURL,
		&app.DocsURL,
		&app.OpenhostIntegrationScore,
		&app.UpdatedAt,
	); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return CatalogApp{}, sql.ErrNoRows
		}
		return CatalogApp{}, fmt.Errorf("scan catalog app: %w", err)
	}
	app.Tags = decodeJSONList(tagsJSON)
	app.Categories = decodeJSONList(categoriesJSON)
	return app, nil
}

func (s *Store) CreatePublish(ctx context.Context, publish Publish) error {
	now := nowString()
	if publish.CreatedAt == "" {
		publish.CreatedAt = now
	}
	if publish.UpdatedAt == "" {
		publish.UpdatedAt = now
	}

	_, err := s.db.ExecContext(
		ctx,
		`INSERT INTO publishes
		(id, source_id, app_id, title, requested_app_name, repo_url, repo_ref, router_app_name, status,
		 error_message, manual_install_url, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		publish.ID,
		publish.SourceID,
		publish.AppID,
		publish.Title,
		publish.RequestedAppName,
		publish.RepoURL,
		publish.RepoRef,
		publish.RouterAppName,
		publish.Status,
		publish.ErrorMessage,
		publish.ManualInstallURL,
		publish.CreatedAt,
		publish.UpdatedAt,
	)
	if err != nil {
		return fmt.Errorf("insert publish: %w", err)
	}
	return nil
}

func (s *Store) GetPublish(ctx context.Context, publishID string) (Publish, error) {
	row := s.db.QueryRowContext(
		ctx,
		`SELECT
			id, source_id, app_id, title, requested_app_name, repo_url, repo_ref, router_app_name, status,
			error_message, manual_install_url, created_at, updated_at
		 FROM publishes
		 WHERE id = ?`,
		publishID,
	)

	var p Publish
	if err := row.Scan(
		&p.ID,
		&p.SourceID,
		&p.AppID,
		&p.Title,
		&p.RequestedAppName,
		&p.RepoURL,
		&p.RepoRef,
		&p.RouterAppName,
		&p.Status,
		&p.ErrorMessage,
		&p.ManualInstallURL,
		&p.CreatedAt,
		&p.UpdatedAt,
	); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Publish{}, sql.ErrNoRows
		}
		return Publish{}, fmt.Errorf("scan publish: %w", err)
	}

	return p, nil
}

func (s *Store) UpdatePublish(ctx context.Context, publish Publish) error {
	publish.UpdatedAt = nowString()
	_, err := s.db.ExecContext(
		ctx,
		`UPDATE publishes
		 SET router_app_name = ?, status = ?, error_message = ?, manual_install_url = ?, updated_at = ?
		 WHERE id = ?`,
		publish.RouterAppName,
		publish.Status,
		publish.ErrorMessage,
		publish.ManualInstallURL,
		publish.UpdatedAt,
		publish.ID,
	)
	if err != nil {
		return fmt.Errorf("update publish: %w", err)
	}
	return nil
}

func (s *Store) GetSetting(ctx context.Context, key string) (string, error) {
	var value string
	err := s.db.QueryRowContext(ctx, `SELECT value FROM settings WHERE key = ?`, key).Scan(&value)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return "", nil
		}
		return "", fmt.Errorf("get setting %q: %w", key, err)
	}
	return value, nil
}

func (s *Store) SetSetting(ctx context.Context, key, value string) error {
	_, err := s.db.ExecContext(
		ctx,
		`INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value`,
		key, value,
	)
	if err != nil {
		return fmt.Errorf("set setting %q: %w", key, err)
	}
	return nil
}

func scanSource(row interface {
	Scan(dest ...any) error
}) (Source, error) {
	var src Source
	var enabled int
	if err := row.Scan(
		&src.ID,
		&src.Name,
		&src.URL,
		&enabled,
		&src.LastSyncAt,
		&src.LastError,
		&src.CreatedAt,
		&src.UpdatedAt,
	); err != nil {
		return Source{}, err
	}
	src.Enabled = enabled == 1
	return src, nil
}

func decodeJSONList(raw string) []string {
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	var out []string
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		return nil
	}
	return out
}

func nowString() string {
	return time.Now().UTC().Format(time.RFC3339)
}

func boolToInt(v bool) int {
	if v {
		return 1
	}
	return 0
}
