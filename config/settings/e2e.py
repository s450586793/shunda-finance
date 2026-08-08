from pathlib import Path

from config.e2e_paths import validate_e2e_paths

E2E_PATHS = validate_e2e_paths(Path(__file__).resolve().parents[2])

from .dev import *

E2E_RESULTS_ROOT = E2E_PATHS.results_root
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": E2E_PATHS.database,
    },
}
MEDIA_ROOT = E2E_PATHS.media
STATIC_ROOT = E2E_PATHS.static
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
