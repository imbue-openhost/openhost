package store

import (
	"context"
	"path/filepath"
	"testing"
)

// TestIntegrationScoreRoundTrip verifies that the
// openhost_integration_score column stores and reads back correctly,
// and that apps without a score round-trip as zero.
func TestIntegrationScoreRoundTrip(t *testing.T) {
	dir := t.TempDir()
	store, err := Open(filepath.Join(dir, "catalog.db"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	defer store.Close()

	ctx := context.Background()
	if err := store.Init(ctx); err != nil {
		t.Fatalf("init: %v", err)
	}

	if err := store.CreateSource(ctx, Source{
		ID:      "official",
		Name:    "OpenHost Official",
		URL:     "https://example.invalid/catalog.json",
		Enabled: true,
	}); err != nil {
		t.Fatalf("create source: %v", err)
	}
	if err := store.MarkSourceSynced(ctx, "official", "OpenHost Official"); err != nil {
		t.Fatalf("mark synced: %v", err)
	}

	apps := []CatalogApp{
		{
			SourceID:                 "official",
			AppID:                    "searxng",
			Title:                    "SearXNG",
			Description:              "Privacy-respecting metasearch",
			RepoURL:                  "https://example.invalid/searxng",
			OpenhostIntegrationScore: 5,
		},
		{
			SourceID:    "official",
			AppID:       "unrated",
			Title:       "Unrated",
			Description: "No integration score set",
			RepoURL:     "https://example.invalid/unrated",
		},
	}
	if err := store.ReplaceCatalogAppsForSource(ctx, "official", apps); err != nil {
		t.Fatalf("replace apps: %v", err)
	}

	got5, err := store.GetCatalogApp(ctx, "official", "searxng")
	if err != nil {
		t.Fatalf("get rated app: %v", err)
	}
	if got5.OpenhostIntegrationScore != 5 {
		t.Errorf("rated app: got score %d, want 5", got5.OpenhostIntegrationScore)
	}

	got0, err := store.GetCatalogApp(ctx, "official", "unrated")
	if err != nil {
		t.Fatalf("get unrated app: %v", err)
	}
	if got0.OpenhostIntegrationScore != 0 {
		t.Errorf("unrated app: got score %d, want 0", got0.OpenhostIntegrationScore)
	}
}

// TestListCatalogAppsOrderedByScore confirms that ListCatalogApps
// returns higher-rated apps first, with alphabetical title as the
// tiebreaker and unrated apps at the bottom.
func TestListCatalogAppsOrderedByScore(t *testing.T) {
	dir := t.TempDir()
	store, err := Open(filepath.Join(dir, "catalog.db"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	defer store.Close()

	ctx := context.Background()
	if err := store.Init(ctx); err != nil {
		t.Fatalf("init: %v", err)
	}
	if err := store.CreateSource(ctx, Source{
		ID:      "s",
		Name:    "S",
		URL:     "https://example.invalid/s.json",
		Enabled: true,
	}); err != nil {
		t.Fatalf("create source: %v", err)
	}
	if err := store.MarkSourceSynced(ctx, "s", "S"); err != nil {
		t.Fatalf("mark synced: %v", err)
	}

	apps := []CatalogApp{
		{SourceID: "s", AppID: "a1", Title: "A1", RepoURL: "https://example.invalid/a1", OpenhostIntegrationScore: 2},
		{SourceID: "s", AppID: "a2", Title: "A2", RepoURL: "https://example.invalid/a2", OpenhostIntegrationScore: 5},
		{SourceID: "s", AppID: "a3", Title: "A3", RepoURL: "https://example.invalid/a3", OpenhostIntegrationScore: 0},
		{SourceID: "s", AppID: "a4", Title: "A4", RepoURL: "https://example.invalid/a4", OpenhostIntegrationScore: 5},
	}
	if err := store.ReplaceCatalogAppsForSource(ctx, "s", apps); err != nil {
		t.Fatalf("replace apps: %v", err)
	}

	got, err := store.ListCatalogApps(ctx, AppListFilter{})
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	wantOrder := []string{"a2", "a4", "a1", "a3"}
	if len(got) != len(wantOrder) {
		t.Fatalf("want %d apps, got %d", len(wantOrder), len(got))
	}
	for i, id := range wantOrder {
		if got[i].AppID != id {
			ids := make([]string, len(got))
			for j, a := range got {
				ids[j] = a.AppID
			}
			t.Fatalf("order mismatch at index %d: got %v, want %v", i, ids, wantOrder)
		}
	}
}

// TestIntegrationMigrationIdempotent checks that re-running Init
// against an already-migrated database is a no-op rather than
// erroring on the duplicate column ALTERs.
func TestIntegrationMigrationIdempotent(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "catalog.db")
	store1, err := Open(path)
	if err != nil {
		t.Fatalf("open 1: %v", err)
	}
	if err := store1.Init(context.Background()); err != nil {
		t.Fatalf("init 1: %v", err)
	}
	store1.Close()

	store2, err := Open(path)
	if err != nil {
		t.Fatalf("open 2: %v", err)
	}
	defer store2.Close()
	if err := store2.Init(context.Background()); err != nil {
		t.Fatalf("init 2 (migration not idempotent): %v", err)
	}
}
