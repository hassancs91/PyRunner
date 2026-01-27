# Security Audit: core/services

**Audit Date**: 2026-01-26
**Scope**: `core/services/` directory
**Purpose**: Pre-release security scan for open source publication

---

## Summary

| Severity | Count | Status |
|----------|-------|--------|
| CRITICAL | 1 | Pending |
| HIGH | 3 | Pending |
| MEDIUM | 6 | Pending |
| LOW | 10 | Pending |

---

## CRITICAL

### 1. SSRF via Unvalidated Webhook URLs
- **File**: `core/services/notification_service.py:202-217`
- **Function**: `_send_webhook_notification()`
- **Issue**: Sends POST requests to user-supplied URLs without validation
- **Risk**: Attackers can probe internal networks, access localhost services, exfiltrate data
- **Fix**:
  ```python
  # Add URL validation before requests.post()
  from urllib.parse import urlparse
  import ipaddress

  def _is_safe_url(url: str) -> bool:
      parsed = urlparse(url)
      if parsed.scheme not in ('https',):  # or allow 'http' if needed
          return False
      try:
          ip = ipaddress.ip_address(parsed.hostname)
          if ip.is_private or ip.is_loopback or ip.is_reserved:
              return False
      except ValueError:
          pass  # hostname, not IP - resolve and check
      return True
  ```

---

## HIGH

### 2. Path Traversal in Environment Service
- **File**: `core/services/environment_service.py:187-209`
- **Issue**: `os.path.join()` with user-supplied paths lacks path normalization
- **Risk**: `../../../etc/passwd` style attacks could escape `ENVIRONMENTS_ROOT`
- **Fix**:
  ```python
  def _safe_path(base: str, user_path: str) -> str:
      full_path = os.path.normpath(os.path.join(base, user_path))
      if not full_path.startswith(os.path.normpath(base) + os.sep):
          raise ValueError("Path traversal detected")
      return full_path
  ```

### 3. Weak Package Spec Validation
- **File**: `core/services/environment_service.py:163-184`
- **Issue**: Package validation regex allows `@` URLs (e.g., `package@https://evil.com/malicious`)
- **Risk**: Malicious package installation from arbitrary URLs
- **Fix**: Reject `@` URL syntax entirely or validate against PyPI allowlist

### 4. Encryption Key Info Disclosure
- **File**: `core/services/encryption_service.py:56-60`
- **Issue**: Error message reveals key generation command
- **Risk**: Aids attackers in understanding system architecture
- **Fix**: Replace with generic error: `"Encryption not configured. See documentation."`

---

## MEDIUM

### 5. Silent SMTP Decryption Failure
- **File**: `core/services/notification_service.py:125-141`
- **Issue**: Decryption failure silently uses empty password
- **Fix**: Raise exception or log warning that notifications will fail

### 6. Race Condition in Temp Files
- **File**: `core/services/environment_service.py:462-501`
- **Issue**: `NamedTemporaryFile(delete=False)` with manual cleanup
- **Fix**: Use `tempfile.mkstemp()` with `os.chmod(0o600)` or use `with` context properly

### 7. Raw SQL Query
- **File**: `core/services/setup_service.py:69-71`
- **Issue**: Direct SQL: `SELECT name FROM sqlite_master WHERE type='table'...`
- **Fix**: Use Django's `connection.introspection.table_names()`

### 8. Secrets Metadata in Backups
- **File**: `core/services/backup_service.py:212-225`
- **Issue**: Backup exports secret metadata (description, creator email)
- **Fix**: Add option to exclude secrets or warn about sensitivity

### 9. No Webhook Retry Limits
- **File**: `core/services/notification_service.py:207-217`
- **Issue**: No rate limiting on webhook retries
- **Fix**: Implement max retry count (e.g., 3) with exponential backoff

### 10. Overly Broad Exception Handling
- **Files**: Multiple services
- **Issue**: `except Exception` masks security-relevant errors
- **Fix**: Catch specific exceptions (IOError, ValueError, etc.)

---

## LOW

### 11. Verbose Error Messages
- **File**: `core/services/backup_service.py:523-524`
- **Issue**: Full exception messages returned to users
- **Fix**: Return generic error, log details server-side

### 12. No Query Bounds
- **File**: `core/services/log_service.py:79-87`
- **Issue**: Limit/offset parameters not validated
- **Fix**: Cap limit at reasonable maximum (e.g., 1000)

### 13. No Log Rotation
- **File**: `core/services/log_service.py`
- **Issue**: Logs can grow unbounded
- **Fix**: Implement size-based rotation or integrate with logging.handlers

### 14. No Rate Limiting on Setup
- **File**: `core/services/setup_service.py:106-126`
- **Issue**: Admin user creation has no rate limiting
- **Fix**: Add attempt tracking and lockout

### 15. Unvalidated Schedule Times
- **File**: `core/services/schedule_service.py:96-100`
- **Issue**: `time_str.split(":")` without try-except
- **Fix**: Wrap in try-except, validate format before parsing

### 16. Fixed Subprocess Timeouts
- **Files**: `environment_service.py`, `setup_service.py`
- **Issue**: Hardcoded timeouts could be abused
- **Fix**: Make configurable with upper bounds

### 17. Service-Level Permission Bypass
- **File**: `core/services/backup_service.py:456-524`
- **Issue**: No permission checks in service layer
- **Fix**: Ensure views always check permissions; consider service-level checks

### 18. Info Disclosure in Logs
- **Files**: `notification_service.py`, `environment_service.py`
- **Issue**: Full stderr/exception details logged
- **Fix**: Sanitize sensitive data before logging

### 19. User-Supplied Python Paths
- **File**: `core/services/environment_service.py:94-100`
- **Issue**: Python executable paths not fully validated
- **Fix**: Verify against known Python installations

### 20. Exception Messages to Users
- **File**: `core/services/backup_service.py`
- **Issue**: `str(e)` exposed in API responses
- **Fix**: Use error codes with generic messages

---

## Remediation Priority

### Before Release (Critical + High)
- [ ] #1 - SSRF protection for webhooks
- [ ] #2 - Path traversal protection
- [ ] #3 - Package spec validation
- [ ] #4 - Encryption error sanitization

### Should Fix (Medium)
- [ ] #5 - SMTP decryption failure handling
- [ ] #6 - Temp file race condition
- [ ] #7 - Raw SQL replacement
- [ ] #8 - Secrets backup handling
- [ ] #9 - Webhook retry limits
- [ ] #10 - Specific exception handling

### Can Fix Later (Low)
- [ ] #11-20 - Various improvements

---

## Files Affected

| File | Issues |
|------|--------|
| `notification_service.py` | #1, #5, #9, #18 |
| `environment_service.py` | #2, #3, #6, #19 |
| `encryption_service.py` | #4 |
| `setup_service.py` | #7, #14 |
| `backup_service.py` | #8, #11, #17, #20 |
| `log_service.py` | #12, #13 |
| `schedule_service.py` | #15 |
