import os

APP_NAME = os.environ["OPENHOST_APP_NAME"]
ZONE_DOMAIN = os.environ["OPENHOST_ZONE_DOMAIN"]
MY_REDIRECT_DOMAIN = os.environ["OPENHOST_MY_REDIRECT_DOMAIN"]
APP_TOKEN = os.environ.get("OPENHOST_APP_TOKEN", "")
ROUTER_URL = os.environ.get("OPENHOST_ROUTER_URL", "")

OAUTH_REDIRECT_URI = f"https://{MY_REDIRECT_DOMAIN}/{APP_NAME}/callback"
OAUTH_SERVICE_URL = "github.com/imbue-openhost/openhost/services/oauth"
