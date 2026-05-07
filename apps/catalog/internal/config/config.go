package config

import (
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	ListenAddr        string
	AppName           string
	AppBasePath       string
	DBPath            string
	RouterURL         string
	RouterToken       string
	AppToken          string
	AllowHTTPRepoURLs bool
	AllowFileRepoURLs bool
	RequestTimeout    time.Duration
	DefaultSourceURL  string
}

func Load() Config {
	return Config{
		ListenAddr:        envOrDefault("LISTEN_ADDR", ":8080"),
		AppName:           envOrDefault("OPENHOST_APP_NAME", "openhost-catalog"),
		AppBasePath:       strings.TrimSpace(os.Getenv("OPENHOST_APP_BASE_PATH")),
		DBPath:            defaultDBPath(),
		RouterURL:         strings.TrimRight(envOrDefault("OPENHOST_ROUTER_URL", "http://host.docker.internal:8080"), "/"),
		RouterToken:       strings.TrimSpace(os.Getenv("APP_REPO_ROUTER_TOKEN")),
		AppToken:          strings.TrimSpace(os.Getenv("OPENHOST_APP_TOKEN")),
		AllowHTTPRepoURLs: boolEnv("CATALOG_ALLOW_HTTP_REPO_URLS", false),
		AllowFileRepoURLs: boolEnv("CATALOG_ALLOW_FILE_URLS", false),
		RequestTimeout:    timeoutFromEnv(),
		DefaultSourceURL:  envOrDefault("DEFAULT_SOURCE_URL", "https://raw.githubusercontent.com/imbue-openhost/openhost-apps/main/catalog.json"),
	}
}

func defaultDBPath() string {
	if v := strings.TrimSpace(os.Getenv("OPENHOST_SQLITE_main")); v != "" {
		return v
	}
	if v := strings.TrimSpace(os.Getenv("CATALOG_DB_PATH")); v != "" {
		return v
	}
	return "./catalog.db"
}

func timeoutFromEnv() time.Duration {
	v := strings.TrimSpace(os.Getenv("CATALOG_HTTP_TIMEOUT_SECONDS"))
	if v == "" {
		return 10 * time.Second
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		return 10 * time.Second
	}
	return time.Duration(n) * time.Second
}

func boolEnv(key string, fallback bool) bool {
	v := strings.TrimSpace(strings.ToLower(os.Getenv(key)))
	if v == "" {
		return fallback
	}
	switch v {
	case "1", "true", "yes", "y", "on":
		return true
	case "0", "false", "no", "n", "off":
		return false
	default:
		return fallback
	}
}

func envOrDefault(key, fallback string) string {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	return v
}
