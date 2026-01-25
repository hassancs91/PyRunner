# PyRunner Update Guide

This guide explains how to safely update your PyRunner installation to a new version while preserving all your data.

## Checking Your Current Version

Your current version is displayed in two places:
- **Sidebar footer** - Always visible at the bottom of the navigation sidebar
- **Settings > System** - Shows version along with other system information

## Quick Update

```bash
# Stop the current container
docker-compose down

# Pull/build the new version
docker-compose build

# Start with the new version
docker-compose up -d
```

That's it! Your data is automatically preserved.

---

## What Happens During an Update

When you start a new container, PyRunner automatically:

1. **Runs database migrations** - Any new database schema changes are applied
2. **Preserves your data** - The `pyrunner_data` volume keeps all your data safe:
   - User accounts
   - Scripts and their code
   - Schedules and run history
   - Secrets (encrypted)
   - Python environments
   - All settings

3. **Skips redundant setup** - Environment creation and other one-time setup steps are skipped if already completed

---

## Critical: Save Your ENCRYPTION_KEY

PyRunner uses an encryption key to secure your stored secrets (API keys, passwords, etc.).

### First-Time Setup

On first run, if no `ENCRYPTION_KEY` is provided, one is auto-generated and printed in the logs:

```
==================================================
  Generated ENCRYPTION_KEY:
  your-generated-key-here
==================================================

  *** IMPORTANT: Save this key! ***
```

**You MUST save this key!** Without it, you cannot access your encrypted secrets after a container recreation.

### Persisting Your Key

Add the key to your `docker-compose.yml`:

```yaml
services:
  pyrunner:
    environment:
      - ENCRYPTION_KEY=your-saved-key-here
```

Or use a `.env` file:

```env
ENCRYPTION_KEY=your-saved-key-here
```

---

## Backup Before Major Updates

While updates are designed to be safe, it's good practice to backup before major version updates.

### Create a Backup

1. Go to **Settings > Backup** in the PyRunner web interface
2. Click **Download Backup**
3. Save the JSON file securely

### What's Included in Backups

- Global settings
- All scripts and their code
- Schedules
- Run history
- Secrets (encrypted - requires same ENCRYPTION_KEY to restore)
- User accounts (without passwords)

### Restore from Backup

1. Go to **Settings > Backup**
2. Upload your backup file
3. Review the preview
4. Confirm restore

---

## Troubleshooting

### Check Migration Status

```bash
docker exec pyrunner python manage.py showmigrations
```

All migrations should show `[X]` indicating they're applied.

### Force Re-run Setup

If you need to re-run the setup process:

```bash
docker exec pyrunner python manage.py setup --force
```

### View Container Logs

```bash
docker-compose logs -f pyrunner
```

### Common Issues

| Issue | Solution |
|-------|----------|
| "Secrets not decrypting" | ENCRYPTION_KEY changed or missing. Restore the original key. |
| "Migration errors" | Check logs for specific error. May need to restore from backup. |
| "Container won't start" | Check logs with `docker-compose logs`. Verify volume permissions. |

---

## Version-Specific Notes

### v1.1.1
- Bug fixes and improvements

### v1.1.0
- Added version display in sidebar
- Fixed migrations not running on container updates
- Improved Settings page tab styling

### v1.0.0
- Initial release with worker settings, tags, and backup/restore

---

## Data Locations

All persistent data is stored in the `pyrunner_data` Docker volume, mapped to `/app/data/` inside the container:

```
/app/data/
├── db.sqlite3          # Database (users, scripts, settings, etc.)
├── environments/       # Python virtual environments
│   └── default/        # Default environment
└── workdir/            # Script execution workspace
```

---

## Rolling Back

If you need to roll back to a previous version:

1. Stop the container: `docker-compose down`
2. Restore your backup (if needed)
3. Change the image tag in `docker-compose.yml` to the previous version
4. Start: `docker-compose up -d`

Note: Rolling back after migrations have run may cause issues. Always backup before updating.
