"""Phase A: a shared disk-space circuit breaker used by EVERY writer that
persists to the Docker volume -- on-demand scan writes today, and any
future background collector job (Phase B) -- so a user actively using the
app is never blocked by their own background collector, but neither path
can ever be allowed to push the volume's disk to zero.

PURELY a pause-new-writes safety valve: this module never deletes
anything, never truncates anything, and never touches existing rows. It
only ever answers the question "is it safe to write one more row right
now" for the given write-kind, each with its own documented threshold.

Configurable via env vars (conservative defaults):
  TIP_STORAGE_GUARD_MOUNT              -- directory whose disk_usage()
                                          represents the real volume (e.g.
                                          /srv/data in Docker). Defaults to
                                          the directory containing the
                                          configured TIP_DB_PATH.
  TIP_STORAGE_GUARD_ONDEMAND_MIN_MB    -- on-demand writes pause below this
                                          many MB free. Default 500.
  TIP_STORAGE_GUARD_BACKGROUND_MIN_MB  -- background-job writes pause below
                                          this many MB free (deliberately
                                          MUCH more conservative than
                                          on-demand, so the collector backs
                                          off long before an active user
                                          could ever be blocked). Default
                                          2048 (2GB).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ONDEMAND = "on_demand"
BACKGROUND = "background"

DEFAULT_ONDEMAND_MIN_MB = 500
DEFAULT_BACKGROUND_MIN_MB = 2048

_VALID_KINDS = (ONDEMAND, BACKGROUND)


@dataclass
class StorageGuardResult:
    allowed: bool
    kind: str
    free_mb: float
    threshold_mb: float
    mount_point: str

    @property
    def reason(self) -> Optional[str]:
        if self.allowed:
            return None
        return (
            f"Storage guard paused {self.kind} writes: only {self.free_mb:.1f}MB free at "
            f"{self.mount_point!r}, below the {self.threshold_mb:.0f}MB {self.kind} threshold. "
            "No existing data was touched or deleted -- this only pauses NEW writes until "
            "free space recovers."
        )


class StorageGuard:
    """Checks real `shutil.disk_usage()` against the actual mount point
    backing the persistent volume, with a different (configurable)
    threshold per write-kind."""

    def __init__(
        self,
        mount_point: "str | os.PathLike",
        ondemand_min_mb: Optional[float] = None,
        background_min_mb: Optional[float] = None,
    ):
        self.mount_point = str(mount_point)
        self.ondemand_min_mb = (
            ondemand_min_mb
            if ondemand_min_mb is not None
            else float(os.environ.get("TIP_STORAGE_GUARD_ONDEMAND_MIN_MB", DEFAULT_ONDEMAND_MIN_MB))
        )
        self.background_min_mb = (
            background_min_mb
            if background_min_mb is not None
            else float(os.environ.get("TIP_STORAGE_GUARD_BACKGROUND_MIN_MB", DEFAULT_BACKGROUND_MIN_MB))
        )
        if self.background_min_mb < self.ondemand_min_mb:
            raise ValueError(
                "background_min_mb must be >= ondemand_min_mb -- background jobs are required "
                "to pause EARLIER (more conservatively) than on-demand writes, never later."
            )

    def _threshold_mb(self, kind: str) -> float:
        if kind == ONDEMAND:
            return self.ondemand_min_mb
        if kind == BACKGROUND:
            return self.background_min_mb
        raise ValueError(f"unknown storage-guard write kind: {kind!r} (expected one of {_VALID_KINDS})")

    def check_before_write(self, kind: str = ONDEMAND) -> StorageGuardResult:
        """Returns a StorageGuardResult; never raises for a low-disk
        condition (callers decide what "not allowed" means for their own
        route -- e.g. a 507 for on-demand, or just skipping this cycle for
        a background job). Only raises for a genuinely invalid `kind` or
        if the mount point cannot be statted at all (a real
        misconfiguration, not a disk-full condition)."""
        threshold_mb = self._threshold_mb(kind)
        usage = shutil.disk_usage(self.mount_point)
        free_mb = usage.free / (1024 * 1024)
        return StorageGuardResult(
            allowed=free_mb >= threshold_mb,
            kind=kind,
            free_mb=free_mb,
            threshold_mb=threshold_mb,
            mount_point=self.mount_point,
        )


def default_mount_point(db_path: "str | os.PathLike") -> str:
    """The real volume's mount point, inferred from the configured
    TIP_DB_PATH's directory (same pattern as auth.default_token_path) --
    in the real Docker deployment that directory IS /srv/data, the
    tip-data named volume's mount point."""
    d = Path(db_path).resolve().parent
    return str(d)
