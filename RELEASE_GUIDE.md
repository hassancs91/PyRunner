# PyRunner Release Guide (Developer)

This guide documents the process for releasing new versions of PyRunner and pushing Docker images.

## Quick Reference

```bash
# Full release workflow
1. Edit pyrunner/version.py     # Update __version__
2. Edit UPDATE_GUIDE.md         # Add changelog entry
3. git add . && git commit -m "Release vX.X.X"
4. git tag vX.X.X
5. git push origin main --tags
6. docker build -t hasanaboulhasan/pyrunner:X.X.X -t hasanaboulhasan/pyrunner:latest .
7. docker push hasanaboulhasan/pyrunner:X.X.X
8. docker push hasanaboulhasan/pyrunner:latest
```

---

## Detailed Release Process

### 1. Update Version Number

Edit `pyrunner/version.py`:

```python
__version__ = "X.X.X"  # New version
```

**Versioning scheme:**
- **Patch** (1.1.x): Bug fixes, minor improvements
- **Minor** (1.x.0): New features, backwards compatible
- **Major** (x.0.0): Breaking changes

### 2. Update Changelog

Add release notes to `UPDATE_GUIDE.md` under the "Version-Specific Notes" section:

```markdown
### vX.X.X
- Description of changes
- Bug fixes
- New features
```

### 3. Commit Changes

```bash
git add .
git commit -m "Release vX.X.X"
```

### 4. Create Git Tag

```bash
git tag vX.X.X
```

### 5. Push to Remote

```bash
git push origin main --tags
```

### 6. Build Docker Image

Build with both version tag and `latest`:

```bash
docker build -t hasanaboulhasan/pyrunner:X.X.X -t hasanaboulhasan/pyrunner:latest .
```

### 7. Push to Docker Hub

Push both tags:

```bash
docker push hasanaboulhasan/pyrunner:X.X.X
docker push hasanaboulhasan/pyrunner:latest
```

---

## Pre-Release Checklist

- [ ] All tests passing
- [ ] No uncommitted changes (except version bump)
- [ ] Version number updated in `pyrunner/version.py`
- [ ] Changelog updated in `UPDATE_GUIDE.md`
- [ ] Docker builds successfully locally

## Post-Release Verification

1. Check Docker Hub for new tags: https://hub.docker.com/r/hasanaboulhasan/pyrunner/tags
2. Pull and verify: `docker pull hasanaboulhasan/pyrunner:latest`
3. Test the container starts correctly

---

## Docker Hub Details

- **Repository:** `hasanaboulhasan/pyrunner`
- **URL:** https://hub.docker.com/r/hasanaboulhasan/pyrunner

## Rollback

If a release has issues:

```bash
# Remove the tag locally and remotely
git tag -d vX.X.X
git push origin :refs/tags/vX.X.X

# Push previous version as latest
docker pull hasanaboulhasan/pyrunner:PREVIOUS_VERSION
docker tag hasanaboulhasan/pyrunner:PREVIOUS_VERSION hasanaboulhasan/pyrunner:latest
docker push hasanaboulhasan/pyrunner:latest
```
