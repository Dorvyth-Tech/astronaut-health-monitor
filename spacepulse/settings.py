import sys
from pathlib import Path

from decouple import Csv, config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
# Deploys set SECRET_KEY in the environment; the fallback is for local development only.
SECRET_KEY = config(
    'SECRET_KEY',
    default='django-insecure-spacepulse-secret-key-deep-space-mars-2026',
)

# SECURITY WARNING: don't run with debug turned on in production!
# Off unless the DEBUG environment variable (or a .env file) says true, so a deploy
# is safe by default. For local development put DEBUG=True in .env.
DEBUG = config('DEBUG', default=False, cast=bool)

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='*', cast=Csv())

# Login and other form POSTs over https on Render are rejected without this.
CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS', default='https://*.onrender.com', cast=Csv()
)

# DEMO_MODE turns on the shortcuts we only want for a demo: the crew switcher,
# the one-click demo logins and the shared demo password. It is OFF unless the
# DEMO_MODE environment variable (or a .env file) says true. See .env.example.
DEMO_MODE = config('DEMO_MODE', default=False, cast=bool)

# Companion layer 2: an optional AI model that only rephrases the rule-based answers
# (core/llm.py). OFF by default, and always off while running tests so the test
# suite never touches the network. Keys and provider: see .env.example.
COMPANION_LLM_ENABLED = config('COMPANION_LLM_ENABLED', default=False, cast=bool) and 'test' not in sys.argv

# Per-user cap on live LLM calls (core/llm.py). Cache hits do not count.
# Anonymous callers never hit the model. Over the limit, the rule-based
# answers are used. Tests can override these with override_settings.
COMPANION_LLM_RATE_LIMIT = config('COMPANION_LLM_RATE_LIMIT', default=20, cast=int)
COMPANION_LLM_RATE_WINDOW = config('COMPANION_LLM_RATE_WINDOW', default=3600, cast=int)

# Space-weather card (core/donki.py, NASA DONKI). When True the dashboard may make ONE bounded
# refresh call (5 s timeout) if its data is more than 30 minutes old; otherwise it only reads the
# cache and the last stored snapshot. Always off while running tests (no network in tests).
# The NASA key is NASA_API_KEY (falls back to DEMO_KEY); see .env.example.
SPACE_WEATHER_AUTO_REFRESH = config('SPACE_WEATHER_AUTO_REFRESH', default=True, cast=bool) and 'test' not in sys.argv

# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Space-Pulse core & telemetry apps
    'core',
    'telemetry',
    'alerts',
    'environment',
    'companion',     # Pulse chat: conversations, messages, memories
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # Serves the collected static files when running under gunicorn.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    # Every view needs a login unless it is marked @login_not_required
    # (only the login and logout views are). Admin has its own login.
    'django.contrib.auth.middleware.LoginRequiredMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'spacepulse.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.spacepulse_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'spacepulse.wsgi.application'

# Database
# https://docs.djangoproject.com/en/6.1/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# Password validation
# https://docs.djangoproject.com/en/6.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Where LoginRequiredMiddleware sends people who are not logged in.
LOGIN_URL = 'core:login'

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

# Not the Manifest variant: the Tailwind source (static/src/input.css) has an @import that
# the manifest step cannot resolve, which makes collectstatic fail.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage',
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# OpenWeather API Configuration for Environment Monitor
OPENWEATHER_API_KEY = config('OPENWEATHER_API_KEY', default='')
OPENWEATHER_CITY = config('OPENWEATHER_CITY', default='Cape Canaveral,US')

# NASA Space Weather API (DONKI) Configuration
NASA_API_KEY = config('NASA_API_KEY', default='DEMO_KEY')
