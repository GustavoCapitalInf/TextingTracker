"""Local development by default; production requires PostgreSQL and explicit secrets."""
import os
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')
# Vercel production and preview deployments always run in production mode, whatever else is set.
ON_VERCEL = os.getenv('VERCEL_ENV') in ('production', 'preview')
ENVIRONMENT = 'production' if ON_VERCEL else os.getenv('DJANGO_ENV', 'development')
if ENVIRONMENT not in {'development', 'production'}:
    raise ImproperlyConfigured('DJANGO_ENV must be development or production.')
DEBUG = ENVIRONMENT == 'development'
DEMO_MODE = DEBUG and os.getenv('PHONETRACKER_DEMO', '') == '1'
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured('Set DJANGO_SECRET_KEY before production startup.')
    # This key is deliberately suitable ONLY for loopback development.
    SECRET_KEY = 'development-only-phonetracker-do-not-deploy-this-key'
if not DEBUG and (len(SECRET_KEY) < 50 or SECRET_KEY.startswith('development-')):
    raise ImproperlyConfigured('Production requires a random secret of at least 50 characters.')

ALLOWED_HOSTS = [v.strip() for v in os.getenv('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1]' if DEBUG else '').split(',') if v.strip()]
CSRF_TRUSTED_ORIGINS = [v.strip() for v in os.getenv('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',') if v.strip()]
if ON_VERCEL:
    # Vercel supplies this project's own domains: the production domain, the branch URL and
    # this deployment's URL. A custom domain still goes in DJANGO_ALLOWED_HOSTS.
    for name in ('VERCEL_PROJECT_PRODUCTION_URL', 'VERCEL_BRANCH_URL', 'VERCEL_URL'):
        host = os.getenv(name, '').strip().lower()
        if host and host not in ALLOWED_HOSTS:
            ALLOWED_HOSTS.append(host)
            CSRF_TRUSTED_ORIGINS.append(f'https://{host}')
if not DEBUG and (not ALLOWED_HOSTS or '*' in ALLOWED_HOSTS):
    raise ImproperlyConfigured('Set explicit DJANGO_ALLOWED_HOSTS for production.')
INSTALLED_APPS = [
    'django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
    'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
    'tracker.apps.TrackerConfig',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'tracker.texting_lists.TextingListMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'tracker.middleware.AccountSessionMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'tracker.middleware.PrivacyHeadersMiddleware',
]
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
        'tracker.context_processors.workspace',
    ]},
}]
WSGI_APPLICATION = 'config.wsgi.application'
DATA_DIR = Path(os.getenv('PHONETRACKER_DATA_DIR') or str(BASE_DIR / '.local'))
database_url = os.getenv('DATABASE_URL', '')
if database_url:
    parsed = urlparse(database_url)
    if parsed.scheme not in ('postgres', 'postgresql'):
        raise ImproperlyConfigured('DATABASE_URL must use PostgreSQL.')
    options = {}
    query = parse_qs(parsed.query)
    if 'sslmode' in query:
        options['sslmode'] = query['sslmode'][0]
    # A pooled URL (PgBouncer in transaction mode, such as Neon's "-pooler" host) hands each
    # transaction to any server connection, so server-side prepared statements and cursors
    # must be off. The phone-write lock is transaction-scoped and works through a pooler.
    pooled = '-pooler.' in (parsed.hostname or '') or os.getenv('DATABASE_POOLED', '') == '1'
    if pooled:
        options['prepare_threshold'] = None
    DATABASES = {'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': unquote(parsed.path.lstrip('/')),
        'USER': unquote(parsed.username or ''), 'PASSWORD': unquote(parsed.password or ''),
        'HOST': parsed.hostname or '127.0.0.1', 'PORT': parsed.port or 5432,
        'CONN_MAX_AGE': 60, 'CONN_HEALTH_CHECKS': True, 'OPTIONS': options,
        'DISABLE_SERVER_SIDE_CURSORS': pooled,
    }}
else:
    if not DEBUG:
        raise ImproperlyConfigured('Production requires DATABASE_URL pointing to PostgreSQL.')
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': DATA_DIR / 'development.sqlite3', 'OPTIONS': {'timeout': 20}}}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LANGUAGE_CODE = 'en-us'
TIME_ZONE = os.getenv('COMPANY_TIME_ZONE', 'America/New_York')
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 30 * 60
SESSION_SAVE_EVERY_REQUEST = False  # Status polling must not extend inactivity.
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG and os.getenv('DJANGO_SECURE_SSL_REDIRECT', 'true').lower() == 'true'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if not DEBUG else None
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
X_FRAME_OPTIONS = 'DENY'
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 6 * 1024 * 1024
# Vercel refuses request bodies over 4.5 MB, so stay under it there (10,000 numbers is ~0.2 MB).
IMPORT_MAX_BYTES = (4 if ON_VERCEL else 5) * 1024 * 1024
IMPORT_MAX_ROWS = 10000
IMPORT_MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
LOGIN_MAX_ATTEMPTS = 5  # per username, from one address
LOGIN_ACCOUNT_MAX_ATTEMPTS = 25  # per username, from all addresses
LOGIN_ADDRESS_MAX_ATTEMPTS = 50  # per address, across usernames
LOGIN_WINDOW_SECONDS = 15 * 60
# Vercel overwrites x-vercel-forwarded-for / x-real-ip with the visitor's address, so sign-in
# limits can trust them there; elsewhere only a loopback proxy's X-Forwarded-For is trusted.
TRUST_VERCEL_IP_HEADERS = ON_VERCEL
LOGGING = {'version': 1, 'disable_existing_loggers': False,
           'handlers': {'console': {'class': 'logging.StreamHandler'}},
           'root': {'handlers': ['console'], 'level': 'WARNING'}}
