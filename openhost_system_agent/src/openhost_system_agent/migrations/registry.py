from openhost_system_agent.migrations.base import SystemMigration

# Numbered migrations in apply order. Versions MUST start at 2 and be
# contiguous. v1 is the baseline produced by ansible setup.yml.
REGISTRY: list[SystemMigration] = []
