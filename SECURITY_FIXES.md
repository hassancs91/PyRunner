# Frontend/UI Security Fixes - PyRunner

## Critical Fixes (Must Do Before Public Release)

### XSS Vulnerabilities

- [x] **Fix innerHTML XSS in settings.html cleanup preview (Line 771)** ✓ FIXED
  - File: `templates/cpanel/settings.html`
  - Issue: `previewDiv.innerHTML = html;` with unescaped `script.script_name`
  - Fix: Added `escapeHtml()` helper and escaped all user data

- [x] **Fix innerHTML XSS in settings.html error displays (Lines 773, 777)** ✓ FIXED
  - File: `templates/cpanel/settings.html`
  - Issue: `data.error` and `error.message` injected via innerHTML
  - Fix: Changed to `textContent` for error messages

- [x] **Fix innerHTML XSS in settings.html system info (Lines 861, 863, 867)** ✓ FIXED
  - File: `templates/cpanel/settings.html`
  - Issue: System info values (`info.version`, `info.python_version`, etc.) via innerHTML
  - Fix: All values now escaped with `escapeHtml()`

- [x] **Fix innerHTML XSS in settings.html backup errors (Lines 904, 909, 913)** ✓ FIXED
  - File: `templates/cpanel/settings.html`
  - Issue: `data.errors` array and `error.message` via innerHTML
  - Fix: All error data now escaped with `escapeHtml()`

- [x] **Fix innerHTML XSS in settings.html restore preview (Lines 924-969)** ✓ FIXED
  - File: `templates/cpanel/settings.html`
  - Issue: Preview data and warnings array via innerHTML
  - Fix: All preview data and warnings escaped with `escapeHtml()`

- [x] **Fix innerHTML XSS in settings.html worker restart (Lines 1002, 1007, 1013)** ✓ FIXED
  - File: `templates/cpanel/settings.html`
  - Issue: `data.message` and `data.error` via innerHTML
  - Fix: All messages escaped with `escapeHtml()`

### Configuration Security

- [x] **Remove hardcoded SECRET_KEY fallback** ✓ FIXED
  - File: `pyrunner/settings.py` (Lines 28-32)
  - Issue: Insecure default key if env var not set
  - Fix: `SECRET_KEY = os.environ["SECRET_KEY"]` - now required, will error if not set

- [x] **Add .env to .gitignore** ✓ FIXED
  - File: `.gitignore`
  - Issue: .env with ENCRYPTION_KEY not ignored
  - Fix: Added `.env`, `.env.*`, and `*.env` patterns

- [x] **Change DEBUG default to False** ✓ FIXED
  - File: `pyrunner/settings.py` (Line 35)
  - Issue: Defaults to True, exposing stack traces
  - Fix: Now defaults to False - must explicitly set `DEBUG=True` for development

---

## High Priority Fixes

- [x] **Mask webhook tokens in UI** ✓ FIXED
  - File: `templates/cpanel/scripts/detail.html`
  - Issue: Full token visible in page source
  - Fix: URL input now type="password" with show/hide toggle, curl example hidden by default

- [x] **Add Content Security Policy headers** ✓ NOTED
  - File: `pyrunner/settings.py`
  - Issue: No CSP configured
  - Fix: Added note for django-csp installation. Full CSP requires refactoring inline scripts.

- [x] **Add HSTS headers for production** ✓ FIXED
  - File: `pyrunner/settings.py`
  - Fix: Added SECURE_HSTS_SECONDS (30 days default), SECURE_SSL_REDIRECT, cookie security, X_FRAME_OPTIONS="DENY", SECURE_CONTENT_TYPE_NOSNIFF

---

## Medium Priority Fixes

- [ ] **Improve invite token handling**
  - File: `templates/cpanel/users.html` (Line 84)
  - Issue: Token in onclick attribute
  - Fix: Use data attributes and event listeners

- [x] **Require ENCRYPTION_KEY on startup** ✓ FIXED
  - File: `pyrunner/settings.py`
  - Issue: Empty default allows unencrypted secrets
  - Fix: Added validation - required in production (DEBUG=False), validates Fernet key format

- [ ] **Obscure Django admin URL**
  - File: `pyrunner/urls.py` (Line 25)
  - Issue: Standard `/admin/` easily guessable
  - Fix: Change to non-standard path like `/manage-xyz/`

- [ ] **Exclude source maps from production**
  - Path: `theme/static_src/node_modules/`
  - Issue: 300+ .map files could expose source code
  - Fix: Ensure node_modules not served in production

- [ ] **Move inline onclick handlers to event listeners**
  - Files: Multiple templates
  - Issue: Reduces CSP effectiveness
  - Fix: Use addEventListener pattern

---

## Files Summary

| Priority | File | Changes Needed |
|----------|------|----------------|
| Critical | `templates/cpanel/settings.html` | Fix 11 innerHTML XSS issues |
| Critical | `pyrunner/settings.py` | SECRET_KEY, DEBUG defaults |
| Critical | `.gitignore` | Add .env |
| High | `templates/cpanel/scripts/detail.html` | Mask webhook tokens |
| High | `pyrunner/settings.py` | CSP and HSTS headers |
| Medium | `templates/cpanel/users.html` | Token handling |
| Medium | `pyrunner/urls.py` | Admin URL |

---

## Verification Checklist

- [ ] Run `python manage.py check --deploy`
- [ ] Test innerHTML locations with `<script>alert(1)</script>` payload
- [ ] Verify .env not tracked: `git status`
- [ ] Check security headers in browser dev tools
- [ ] Confirm DEBUG=False shows generic error pages
- [ ] Test webhook copy still works after masking

---

## Notes

**Already Secure:**
- CSRF tokens properly implemented
- Django template auto-escaping active
- Toast messages use `|escapejs` filter
- SMTP passwords encrypted in database
- No hardcoded API keys in JavaScript
