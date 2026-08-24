"""Reusable aspects shared by every deployment step.

Each module here owns one concern and takes an executor (see ssh_executor) as
its first argument, so the same code drives any reachable host regardless of
whether the cluster is one VM or twenty.
"""
