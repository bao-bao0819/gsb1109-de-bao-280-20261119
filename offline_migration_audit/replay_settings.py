from os.path import abspath, dirname, join

BASE_DIR = dirname(abspath(__file__))

SECRET_KEY = "replay-only-not-a-secret"
DEBUG = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "simple_history",
    "replay_app",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": join(BASE_DIR, "replay.db"),
    }
}

USE_TZ = False
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
