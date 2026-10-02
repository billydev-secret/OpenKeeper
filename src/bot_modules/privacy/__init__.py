"""Privacy: data-deletion business logic extracted from privacy_cog.

The cog (``bot_modules.privacy.cog``) drives the Discord side of the
``/mydata`` message-deletion flow, and ``deletion_service`` here runs the headless engine
both it and the dashboard's Member Data panel share. This package holds the
parts that
are pure Python — message bucketing, snowflake-time partitioning, progress
bar rendering, ephemeral status text — so they can be unit-tested without
spinning up a Discord client or a fake interaction.

The DB-side purge has its own module under
``bot_modules.privacy.service``.
"""
