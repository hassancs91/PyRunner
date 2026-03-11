"""
Service for managing Python environments and packages.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# Security: Regex patterns for validating package names
# Based on PEP 508 naming conventions
PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")
# Package spec can include version specifiers
PACKAGE_SPEC_PATTERN = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"(\[[\w,]+\])?"  # Optional extras like [dev,test]
    r"(==|>=|<=|>|<|~=|!=|@)?"  # Version operator
    r"[\d\w.*,<>=!~\[\]]*$"  # Version spec
)


class EnvironmentService:
    """
    Manages Python virtual environments and package operations.
    All methods are platform-aware (Windows vs Unix).
    """

    @classmethod
    def _safe_environment_path(cls, base: str, user_path: str) -> str:
        """
        Validate and return safe path within base directory.

        Args:
            base: The base directory (ENVIRONMENTS_ROOT)
            user_path: User-supplied relative path

        Returns:
            The validated full path

        Raises:
            ValueError: If path traversal is detected
        """
        # Normalize and resolve the full path
        full_path = os.path.normpath(os.path.join(base, user_path))
        base_resolved = os.path.normpath(base)

        # Ensure the path stays within the base directory
        if not (full_path.startswith(base_resolved + os.sep) or full_path == base_resolved):
            raise ValueError("Invalid path: path traversal detected")

        return full_path

    @classmethod
    def discover_python_versions(cls) -> list[dict]:
        """
        Discover available Python installations on the system.
        Returns list of dicts with 'path', 'version', 'display' keys.
        """
        pythons = []
        seen_paths = set()

        # On Windows, try the py launcher first
        if os.name == "nt":
            pythons.extend(cls._discover_via_py_launcher())

        # Check current Python (the one running Django)
        current_python = sys.executable
        if current_python and os.path.isfile(current_python):
            version = cls._get_python_version(current_python)
            if version and current_python not in seen_paths:
                pythons.append(
                    {
                        "path": current_python,
                        "version": version,
                        "display": f"Python {version} (current)",
                    }
                )
                seen_paths.add(current_python)

        # Check PATH for python executables
        path_pythons = cls._discover_in_path()
        for p in path_pythons:
            if p["path"] not in seen_paths:
                pythons.append(p)
                seen_paths.add(p["path"])

        return pythons

    @classmethod
    def _discover_via_py_launcher(cls) -> list[dict]:
        """Use Windows py launcher to find Python versions."""
        pythons = []
        try:
            # py -0p lists all installed Pythons with paths
            result = subprocess.run(
                ["py", "-0p"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    # Format: " -V:3.12 *       C:\Python312\python.exe"
                    # or:     " -V:3.11         C:\Python311\python.exe"
                    parts = line.split()
                    if len(parts) >= 2:
                        # Find the path (starts with drive letter on Windows)
                        path = None
                        for part in parts:
                            if len(part) > 2 and part[1] == ":":
                                path = part
                                break
                        if path and os.path.isfile(path):
                            version = cls._get_python_version(path)
                            if version:
                                is_default = "*" in line
                                display = f"Python {version}"
                                if is_default:
                                    display += " (default)"
                                pythons.append(
                                    {
                                        "path": path,
                                        "version": version,
                                        "display": display,
                                    }
                                )
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
            logger.debug(f"py launcher discovery failed: {e}")
        return pythons

    @classmethod
    def _discover_in_path(cls) -> list[dict]:
        """Find Python executables in PATH."""
        pythons = []
        names = ["python3", "python"] if os.name != "nt" else ["python.exe"]

        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        for directory in path_dirs:
            for name in names:
                python_path = os.path.join(directory, name)
                if os.path.isfile(python_path):
                    version = cls._get_python_version(python_path)
                    if version:
                        pythons.append(
                            {
                                "path": python_path,
                                "version": version,
                                "display": f"Python {version}",
                            }
                        )
        return pythons

    @classmethod
    def _get_python_version(cls, python_path: str) -> Optional[str]:
        """Get Python version string from executable."""
        try:
            result = subprocess.run(
                [python_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode == 0:
                # Output: "Python 3.12.0"
                output = result.stdout.strip() or result.stderr.strip()
                if output.startswith("Python "):
                    return output.replace("Python ", "")
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
            logger.debug(f"Failed to get version for {python_path}: {e}")
        return None

    @classmethod
    def validate_package_spec(cls, package_spec: str) -> bool:
        """
        Validate package specification to prevent command injection.
        Returns True if valid, False otherwise.
        """
        if not package_spec or not package_spec.strip():
            return False

        spec = package_spec.strip()

        # Block pip flags (e.g., --index-url, -e, etc.)
        if spec.startswith("-"):
            return False

        # Check for shell metacharacters and null bytes
        # Note: < and > are allowed for version specifiers (>=, <=, etc.)
        dangerous_chars = [";", "&", "|", "`", "$", "(", ")", "{", "}", "\n", "\r", "\0"]
        if any(char in spec for char in dangerous_chars):
            return False

        # Basic package name validation
        # Extract base package name (before any version specifier or extras)
        base_name = re.split(r"[=<>!~\[@]", spec)[0]
        if not base_name or not PACKAGE_NAME_PATTERN.match(base_name):
            return False

        return True

    @classmethod
    def create_environment(
        cls,
        python_path: str,
        env_path: str,
    ) -> tuple[bool, str]:
        """
        Create a new virtual environment.

        Args:
            python_path: Path to Python executable to use
            env_path: Relative path within ENVIRONMENTS_ROOT

        Returns:
            Tuple of (success: bool, message: str)
        """
        # Validate path to prevent directory traversal attacks
        try:
            full_path = cls._safe_environment_path(settings.ENVIRONMENTS_ROOT, env_path)
        except ValueError as e:
            return False, str(e)

        # Check if path already exists
        if os.path.exists(full_path):
            return False, f"Path already exists: {full_path}"

        # Ensure parent directory exists
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        try:
            cmd = [python_path, "-m", "venv", full_path]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=creationflags,
            )

            if result.returncode != 0:
                error_msg = result.stderr or result.stdout or "Unknown error"
                return False, f"Failed to create venv: {error_msg}"

            # Verify the environment was created
            if not os.path.isdir(full_path):
                return False, "Environment directory not created"

            logger.info(f"Created environment at {full_path}")
            return True, "Environment created successfully"

        except subprocess.TimeoutExpired:
            return False, "Timeout creating environment"
        except Exception as e:
            return False, f"Error creating environment: {str(e)}"

    @classmethod
    def delete_environment(cls, environment) -> tuple[bool, str]:
        """
        Delete an environment's venv folder.

        Args:
            environment: Environment model instance

        Returns:
            Tuple of (success: bool, message: str)
        """
        full_path = environment.get_full_path()

        if not os.path.exists(full_path):
            return True, "Environment folder already deleted"

        try:
            shutil.rmtree(full_path)
            logger.info(f"Deleted environment at {full_path}")
            return True, "Environment deleted successfully"
        except PermissionError:
            return False, "Permission denied - cannot delete environment folder"
        except Exception as e:
            return False, f"Error deleting environment: {str(e)}"

    @classmethod
    def get_installed_packages(cls, environment) -> list[dict]:
        """
        Get list of installed packages in an environment.

        Args:
            environment: Environment model instance

        Returns:
            List of dicts with 'name', 'version' keys
        """
        pip_path = environment.get_pip_executable()

        if not os.path.isfile(pip_path):
            logger.warning(f"pip not found at {pip_path}")
            return []

        try:
            cmd = [pip_path, "list", "--format=json"]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=creationflags,
            )

            if result.returncode == 0:
                packages = json.loads(result.stdout)
                return [{"name": p["name"], "version": p["version"]} for p in packages]
            else:
                logger.warning(f"pip list failed: {result.stderr}")
                return []

        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
            logger.error(f"Failed to get installed packages: {e}")
            return []

    @classmethod
    def pip_freeze(cls, environment) -> str:
        """
        Get pip freeze output for requirements.txt format.

        Args:
            environment: Environment model instance

        Returns:
            Requirements.txt content string
        """
        pip_path = environment.get_pip_executable()

        if not os.path.isfile(pip_path):
            return ""

        try:
            cmd = [pip_path, "freeze"]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=creationflags,
            )

            if result.returncode == 0:
                return result.stdout.strip()
            return ""

        except Exception as e:
            logger.error(f"pip freeze failed: {e}")
            return ""

    @classmethod
    def install_package(
        cls, environment, package_spec: str
    ) -> tuple[bool, str, str]:
        """
        Install a package synchronously.

        Args:
            environment: Environment model instance
            package_spec: Package specification (e.g., "requests==2.31.0")

        Returns:
            Tuple of (success: bool, stdout: str, stderr: str)
        """
        if not cls.validate_package_spec(package_spec):
            return False, "", "Invalid package specification"

        pip_path = environment.get_pip_executable()

        if not os.path.isfile(pip_path):
            return False, "", f"pip not found at {pip_path}"

        try:
            cmd = [pip_path, "install", package_spec]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minutes timeout
                creationflags=creationflags,
            )

            success = result.returncode == 0
            if success:
                logger.info(f"Installed {package_spec} in {environment.name}")
            else:
                logger.warning(f"Failed to install {package_spec}: {result.stderr}")

            return success, result.stdout, result.stderr

        except subprocess.TimeoutExpired:
            return False, "", "Installation timed out after 5 minutes"
        except Exception as e:
            return False, "", f"Installation error: {str(e)}"

    @classmethod
    def uninstall_package(
        cls, environment, package_name: str
    ) -> tuple[bool, str, str]:
        """
        Uninstall a package synchronously.

        Args:
            environment: Environment model instance
            package_name: Package name to uninstall

        Returns:
            Tuple of (success: bool, stdout: str, stderr: str)
        """
        if not cls.validate_package_spec(package_name):
            return False, "", "Invalid package name"

        pip_path = environment.get_pip_executable()

        if not os.path.isfile(pip_path):
            return False, "", f"pip not found at {pip_path}"

        try:
            cmd = [pip_path, "uninstall", "-y", package_name]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=creationflags,
            )

            success = result.returncode == 0
            if success:
                logger.info(f"Uninstalled {package_name} from {environment.name}")
            else:
                logger.warning(f"Failed to uninstall {package_name}: {result.stderr}")

            return success, result.stdout, result.stderr

        except subprocess.TimeoutExpired:
            return False, "", "Uninstallation timed out"
        except Exception as e:
            return False, "", f"Uninstallation error: {str(e)}"

    @classmethod
    def install_requirements(
        cls, environment, requirements: str
    ) -> tuple[bool, str, str]:
        """
        Install packages from requirements.txt content.

        Args:
            environment: Environment model instance
            requirements: Content of requirements.txt

        Returns:
            Tuple of (success: bool, stdout: str, stderr: str)
        """
        pip_path = environment.get_pip_executable()

        if not os.path.isfile(pip_path):
            return False, "", f"pip not found at {pip_path}"

        # Validate each line
        for line in requirements.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("-"):
                # Extract package name (before any version specifier)
                pkg_spec = line.split()[0] if line.split() else ""
                if pkg_spec and not cls.validate_package_spec(pkg_spec):
                    return False, "", f"Invalid package specification: {line}"

        # Write requirements to temp file
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write(requirements)
                temp_path = f.name

            cmd = [pip_path, "install", "-r", temp_path]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10 minutes for bulk install
                creationflags=creationflags,
            )

            success = result.returncode == 0
            if success:
                logger.info(f"Installed requirements in {environment.name}")
            else:
                logger.warning(f"Failed to install requirements: {result.stderr}")

            return success, result.stdout, result.stderr

        except subprocess.TimeoutExpired:
            return False, "", "Installation timed out after 10 minutes"
        except Exception as e:
            return False, "", f"Installation error: {str(e)}"
        finally:
            # Clean up temp file
            try:
                os.unlink(temp_path)
            except Exception:
                pass

    @classmethod
    def get_disk_usage(cls, environment) -> int:
        """
        Calculate disk usage of environment folder in bytes.

        Args:
            environment: Environment model instance

        Returns:
            Total size in bytes
        """
        full_path = environment.get_full_path()

        if not os.path.isdir(full_path):
            return 0

        total = 0
        try:
            for dirpath, dirnames, filenames in os.walk(full_path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        total += os.path.getsize(fp)
                    except (OSError, FileNotFoundError):
                        pass
        except Exception as e:
            logger.error(f"Failed to calculate disk usage: {e}")

        return total

    @classmethod
    def format_disk_usage(cls, size_bytes: int) -> str:
        """Format disk usage in human-readable format."""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

    @classmethod
    def get_python_version_from_env(cls, environment) -> Optional[str]:
        """Get Python version from an environment's Python executable."""
        python_path = environment.get_python_executable()
        return cls._get_python_version(python_path)
