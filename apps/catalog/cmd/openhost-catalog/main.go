package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/imbue-openhost/openhost-catalog/internal/catalog"
	"github.com/imbue-openhost/openhost-catalog/internal/config"
	"github.com/imbue-openhost/openhost-catalog/internal/store"
	"github.com/imbue-openhost/openhost-catalog/internal/web"
)

func main() {
	cfg := config.Load()

	st, err := store.Open(cfg.DBPath)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer st.Close()

	if err := st.Init(context.Background()); err != nil {
		log.Fatalf("initialize store schema: %v", err)
	}

	seedDefaultSource(cfg, st)

	handler, err := web.NewServer(cfg, st)
	if err != nil {
		log.Fatalf("initialize web server: %v", err)
	}

	srv := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("openhost-catalog listening on %s", cfg.ListenAddr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server failed: %v", err)
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("shutdown error: %v", err)
	}
}

// seedDefaultSource adds and syncs the default catalog source if no sources
// exist yet and DEFAULT_SOURCE_URL is configured.
func seedDefaultSource(cfg config.Config, st *store.Store) {
	if cfg.DefaultSourceURL == "" {
		return
	}

	ctx := context.Background()
	sources, err := st.ListSources(ctx)
	if err != nil {
		log.Printf("seed: failed to list sources: %v", err)
		return
	}
	if len(sources) > 0 {
		return
	}

	log.Printf("seed: no sources configured, adding default source: %s", cfg.DefaultSourceURL)
	src := store.Source{
		ID:      "official",
		Name:    "OpenHost Official",
		URL:     cfg.DefaultSourceURL,
		Enabled: true,
	}
	if err := st.CreateSource(ctx, src); err != nil {
		log.Printf("seed: failed to create default source: %v", err)
		return
	}

	svc := catalog.NewService(st, &http.Client{Timeout: cfg.RequestTimeout})
	if err := svc.SyncSource(ctx, src.ID); err != nil {
		log.Printf("seed: failed to sync default source: %v", err)
		return
	}
	log.Printf("seed: default source synced successfully")
}
