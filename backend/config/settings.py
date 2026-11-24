"""
Django settings for the Smart Customer Follow-up Worker.

Deliberately minimal: no auth app, no admin, no sessions. The worker is the
subject of this prototype, not the web framework around it.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'dev-only-insecure-key')
DEBUG = os.environ.get('DJANGO_DEBUG', '1') == '1'
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
    if host.strip()
]

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.staticfiles',
    'corsheaders',
    'rest_framework',
    'core',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
]

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {'context_processors': []},
    }
]

if os.environ.get('POSTGRES_DB'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ['POSTGRES_DB'],
            'USER': os.environ.get('POSTGRES_USER', 'postgres'),
            'PASSWORD': os.environ.get('POSTGRES_PASSWORD', ''),
            'HOST': os.environ.get('POSTGRES_HOST', '127.0.0.1'),
            'PORT': os.environ.get('POSTGRES_PORT', '5432'),
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],
    'DEFAULT_PARSER_CLASSES': ['rest_framework.parsers.JSONParser'],
    'UNAUTHENTICATED_USER': None,
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.AllowAny'],
    'EXCEPTION_HANDLER': 'core.views.plain_error_handler',
}

CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        'CORS_ALLOWED_ORIGINS',
        'http://localhost:5175,http://127.0.0.1:5175,http://localhost:5173',
    ).split(',')
    if origin.strip()
]
CORS_ALLOW_HEADERS = ['accept', 'content-type', 'x-owner-name']

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True
TIME_ZONE = 'Asia/Kolkata'
LANGUAGE_CODE = 'en-us'
STATIC_URL = 'static/'

APPEND_SLASH = True

FOLLOW_UP_MIN_INACTIVE_DAYS = int(os.environ.get('FOLLOW_UP_MIN_INACTIVE_DAYS', '30'))
FOLLOW_UP_MAX_INACTIVE_DAYS = int(os.environ.get('FOLLOW_UP_MAX_INACTIVE_DAYS', '180'))

MAX_AUTONOMOUS_DISCOUNT_PCT = int(os.environ.get('MAX_AUTONOMOUS_DISCOUNT_PCT', '0'))
MAX_PROPOSABLE_DISCOUNT_PCT = int(os.environ.get('MAX_PROPOSABLE_DISCOUNT_PCT', '25'))

REQUIRE_MARKETING_CONSENT = os.environ.get('REQUIRE_MARKETING_CONSENT', '1') == '1'

LANGGRAPH_CHECKPOINT_PATH = os.environ.get(
    'LANGGRAPH_CHECKPOINT_PATH', str(BASE_DIR / 'agent_checkpoints.sqlite')
)
WORKER_RECURSION_LIMIT = int(os.environ.get('WORKER_RECURSION_LIMIT', '40'))
TOOL_MAX_ATTEMPTS = int(os.environ.get('TOOL_MAX_ATTEMPTS', '3'))

LLM_PROVIDER = os.environ.get('LLM_PROVIDER', '').strip().lower()
LLM_API_KEY = os.environ.get('LLM_API_KEY', '').strip()
LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta')
LLM_FAST_MODEL = os.environ.get('LLM_FAST_MODEL', 'gemini-flash-lite-latest')
LLM_REASONING_MODEL = os.environ.get('LLM_REASONING_MODEL', 'gemini-3.1-flash-lite')
LLM_FALLBACK_MODELS = [
    model.strip()
    for model in os.environ.get('LLM_FALLBACK_MODELS', 'gemini-flash-lite-latest').split(',')
    if model.strip()
]
LLM_MAX_ATTEMPTS = int(os.environ.get('LLM_MAX_ATTEMPTS', '3'))
LLM_RETRY_BACKOFF_SECONDS = float(os.environ.get('LLM_RETRY_BACKOFF_SECONDS', '1.5'))
LLM_TIMEOUT_SECONDS = float(os.environ.get('LLM_TIMEOUT_SECONDS', '30'))

LLM_PRICING = {
    'gemini-flash-lite-latest': {'in': 0.10, 'out': 0.40},
    'gemini-3.1-flash-lite': {'in': 0.10, 'out': 0.40},
    'gemini-flash-latest': {'in': 0.30, 'out': 1.20},
    'gemini-3.1-pro-preview': {'in': 1.25, 'out': 5.00},
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': 'INFO'},
}
