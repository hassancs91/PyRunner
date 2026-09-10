# PyRun plugins package.
#
# This file makes `plugins` an importable top-level package so installed
# plugins resolve as `plugins.<slug>`. Plugin folders themselves are NOT
# tracked in git (they are uploaded onto the data volume at runtime); see
# .gitignore and docs/PLAN_plugin_system.md.
