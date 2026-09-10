"""
Example plugin views.

Demonstrates the safe "compute in an environment" pattern: the web layer stays
thin and any real work runs as an isolated subprocess in a chosen PyRun
environment's venv via ``run_in_environment`` — never inside the Django process.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from core.models import Environment
from core.plugins import run_in_environment

# A tiny snippet executed inside the selected environment's venv. A real plugin
# would run a bundled .py via ``path=`` (or queue a Script Run) instead.
SNIPPET = """
import sys, platform
print("Python:", sys.version.split()[0])
print("Executable:", sys.executable)
print("Platform:", platform.platform())
"""


@login_required
def index(request):
    environments = Environment.objects.filter(is_active=True)
    result = None
    selected_id = request.POST.get("environment")

    if request.method == "POST" and selected_id:
        env = Environment.objects.filter(pk=selected_id).first()
        if env is not None:
            try:
                exit_code, stdout, stderr = run_in_environment(
                    env, code=SNIPPET, timeout=30
                )
                result = {
                    "env": env,
                    "exit_code": exit_code,
                    "stdout": stdout,
                    "stderr": stderr,
                }
            except Exception as exc:  # environment missing / not built, etc.
                result = {"env": env, "exit_code": -1, "stdout": "", "stderr": str(exc)}

    return render(
        request,
        "example_plugin/index.html",
        {"environments": environments, "result": result, "selected_id": selected_id},
    )
