# PyRunner Implementation Plan

## Project Overview
Self-hosted Python script automation platform for solo developers, indie hackers, and small teams.

**Tech Stack:**
- Backend: Django 6
- Task Queue: django-q2 (ORM broker, no Redis)
- Database: SQLite (default), Postgres (optional)
- Frontend: Django templates + Tailwind CSS
- Auth: Magic link only
- Deployment: Single Docker container

---

## Phase 1 Scope

1. Project setup (Django, django-q2, Tailwind)
2. User auth (magic link only)
3. Environment model + default env creation
4. Script model (paste code only)
5. Manual run mode only
6. Basic executor (subprocess, capture output)
7. Run model + history
8. Cpanel dashboard (list scripts, run, view logs)

---

## Project Structure

```
PyRunner/
├── manage.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── pyrunner/                    # Django project config
│   ├── __init__.py
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── core/                        # Main app
│   ├── models/                  # Modular models
│   │   ├── __init__.py
│   │   ├── user.py
│   │   ├── environment.py
│   │   ├── script.py
│   │   └── run.py
│   ├── views/
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── dashboard.py
│   │   ├── scripts.py
│   │   ├── runs.py
│   │   └── environments.py
│   ├── urls/
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   └── cpanel.py
│   ├── forms.py
│   ├── tasks.py                 # django-q2 tasks
│   ├── executor.py              # Script execution
│   └── management/commands/
│       └── setup_default_env.py
├── templates/
│   ├── base.html
│   ├── auth/
│   │   ├── login.html
│   │   ├── verify.html
│   │   └── magic_link_sent.html
│   └── cpanel/
│       ├── dashboard.html
│       ├── script_create.html
│       ├── script_detail.html
│       ├── script_edit.html
│       ├── run_detail.html
│       └── run_list.html
├── theme/                       # Tailwind (auto-generated)
├── static/css/
├── data/                        # Runtime data (Docker volume)
│   ├── db.sqlite3
│   └── environments/
└── scripts/
    └── entrypoint.sh
```

---

## Implementation Steps

### Step 1: Project Setup
- [x] Create requirements.txt
- [x] Install dependencies
- [x] Create Django project
- [x] Create core app
- [x] Configure settings.py
- [x] Create data directories

### Step 2: Models (Modular)
- [x] core/models/user.py (User, MagicToken)
- [x] core/models/environment.py
- [x] core/models/script.py
- [x] core/models/run.py
- [x] core/models/__init__.py (exports)
- [x] Run migrations

### Step 3: Magic Link Auth
- [x] core/views/auth.py
- [x] core/urls/auth.py
- [x] Auth templates
- [x] Email configuration (Resend API + console backend)

### Step 4: Script Executor
- [x] core/executor.py
- [x] Subprocess execution
- [x] Timeout handling
- [x] Output capture

### Step 5: django-q2 Integration
- [ ] core/tasks.py
- [ ] Q_CLUSTER configuration
- [ ] Async task execution

### Step 6: Tailwind Setup
- [x] django-tailwind init
- [x] Configure content paths
- [x] Build CSS

### Step 7: Views & Templates
- [x] Dashboard view (placeholder)
- [x] Script CRUD views
- [x] Run views
- [x] Environment views
- [x] All templates

### Step 8: Default Environment
- [x] setup_default_env command
- [ ] Auto-create on first run

### Step 9: Docker Deployment
- [ ] Dockerfile
- [ ] docker-compose.yml
- [ ] entrypoint.sh
- [ ] .env.example

---

## URL Structure

```
/                              -> Redirect to /cpanel/
/auth/login/                   -> Magic link request
/auth/verify/<token>/          -> Verify & login
/auth/logout/                  -> Logout
/cpanel/                       -> Dashboard
/cpanel/scripts/               -> Script list
/cpanel/scripts/create/        -> Create script
/cpanel/scripts/<uuid>/        -> Script detail
/cpanel/scripts/<uuid>/edit/   -> Edit script
/cpanel/scripts/<uuid>/run/    -> Run script (POST)
/cpanel/scripts/<uuid>/toggle/ -> Enable/disable (POST)
/cpanel/runs/                  -> All runs
/cpanel/runs/<uuid>/           -> Run detail
/cpanel/environments/          -> Environment list
/cpanel/environments/<uuid>/   -> Environment detail
```

---

## Models Reference

### User
```python
class User(AbstractUser):
    email = EmailField(unique=True)  # USERNAME_FIELD
    is_verified = BooleanField(default=False)
```

### MagicToken
```python
class MagicToken(Model):
    token = CharField(max_length=64, unique=True)
    user = ForeignKey(User)
    email = EmailField()
    created_at = DateTimeField(auto_now_add=True)
    expires_at = DateTimeField()
    used_at = DateTimeField(null=True)
    ip_address = GenericIPAddressField(null=True)
```

### Environment
```python
class Environment(Model):
    id = UUIDField(primary_key=True)
    name = CharField(max_length=100)
    description = TextField(blank=True)
    path = CharField(max_length=255, unique=True)
    python_version = CharField(max_length=20)
    requirements = TextField(blank=True)
    is_default = BooleanField(default=False)
    is_active = BooleanField(default=True)
    created_at = DateTimeField(auto_now_add=True)
    updated_at = DateTimeField(auto_now=True)
    created_by = ForeignKey(User, null=True)
```

### Script
```python
class Script(Model):
    id = UUIDField(primary_key=True)
    name = CharField(max_length=200)
    description = TextField(blank=True)
    code = TextField()
    environment = ForeignKey(Environment, on_delete=PROTECT)
    timeout_seconds = PositiveIntegerField(default=300)
    is_enabled = BooleanField(default=True)
    created_at = DateTimeField(auto_now_add=True)
    updated_at = DateTimeField(auto_now=True)
    created_by = ForeignKey(User, null=True)
```

### Run
```python
class Run(Model):
    class Status(TextChoices):
        PENDING = 'pending'
        RUNNING = 'running'
        SUCCESS = 'success'
        FAILED = 'failed'
        TIMEOUT = 'timeout'
        CANCELLED = 'cancelled'

    id = UUIDField(primary_key=True)
    script = ForeignKey(Script, on_delete=CASCADE)
    status = CharField(choices=Status.choices, default=PENDING)
    exit_code = IntegerField(null=True)
    stdout = TextField(blank=True)
    stderr = TextField(blank=True)
    started_at = DateTimeField(null=True)
    ended_at = DateTimeField(null=True)
    code_snapshot = TextField(blank=True)
    triggered_by = ForeignKey(User, null=True)
    task_id = CharField(max_length=100, blank=True)
    created_at = DateTimeField(auto_now_add=True)
```

---

## Settings Configuration

```python
# Key settings for pyrunner/settings.py

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_q',
    'tailwind',
    'theme',
    'core',
]

AUTH_USER_MODEL = 'core.User'
LOGIN_URL = 'auth:login'
LOGIN_REDIRECT_URL = 'cpanel:dashboard'

# django-q2
Q_CLUSTER = {
    'name': 'PyRunner',
    'workers': 2,
    'timeout': 600,
    'orm': 'default',
}

# PyRunner paths
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / 'data'
ENVIRONMENTS_ROOT = DATA_DIR / 'environments'
SCRIPTS_WORKDIR = DATA_DIR / 'workdir'
```

---

## Future Phases

### Phase 2
- Run modes: Interval, Daily, Cron
- Webhook triggers
- Package management UI

### Phase 3
- Secrets management
- Email notifications
- Webhook notifications

### Phase 4
- Settings UI
- Backup/restore
- Multi-user management
