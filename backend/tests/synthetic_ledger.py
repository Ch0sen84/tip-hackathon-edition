"""An AUTHORITATIVE synthetic provider ledger for collector tests.

LedgerUW extends the verified-contract fake (tests/uw_paginated.PaginatedUW) with a clock and with the
time at which each row becomes visible at the provider:

  _visible_from   UTC datetime before which the provider does not return the row (late reports,
                  provider outages that are backfilled later; None = visible immediately)
  _hidden         True: the provider never returns the row (a permanent provider-side gap)
  _canceled_from  UTC datetime from which the row is served with canceled=True

`truth(...)` answers what a perfect collector must have stored for a session as of a time, so
tests can compare the catalog with the ledger exactly (not just counts)."""
from __future__ import annotations

from datetime import datetime, timezone

from app.ingestion import darkpool_print_key
from tests.uw_paginated import PaginatedUW, _ts

FAR_FUTURE = datetime(2100, 1, 1, tzinfo=timezone.utc)


def _public(row: dict, now: datetime) -> dict:
    out = {k: v for k, v in row.items() if not k.startswith("_")}
    cf = row.get("_canceled_from")
    if cf is not None and now >= cf:
        out["canceled"] = True
    return out


def _visible(row: dict, now: datetime) -> bool:
    if row.get("_hidden"):
        return False
    vf = row.get("_visible_from")
    return vf is None or now >= vf


class LedgerUW(PaginatedUW):
    def __init__(self, *a, now: datetime = FAR_FUTURE, **k):
        super().__init__(*a, **k)
        self.now = now

    def get(self, url, headers=None, params=None, timeout=None):
        saved = self.darkpool, self.flow
        self.darkpool = {t: [_public(r, self.now) for r in rows if _visible(r, self.now)] for t, rows in saved[0].items()}
        self.flow = {t: [_public(r, self.now) for r in rows if _visible(r, self.now)] for t, rows in saved[1].items()}
        try:
            return super().get(url, headers, params, timeout)
        finally:
            self.darkpool, self.flow = saved

    def truth(self, dataset: str, ticker: str, open_utc: datetime, close_utc: datetime, as_of: datetime) -> set:
        """Keys of every row a complete collection must hold for [open, close] as of `as_of`
        (visible, not canceled), in the same identity the catalog deduplicates by."""
        rows = (self.darkpool if dataset == "darkpool" else self.flow).get(ticker.upper(), [])
        field = "executed_at" if dataset == "darkpool" else "created_at"
        out = set()
        for r in rows:
            if not _visible(r, as_of):
                continue
            pub = _public(r, as_of)
            if dataset == "darkpool" and pub.get("canceled"):
                continue
            if not (open_utc <= _ts(pub[field]) <= close_utc):
                continue
            out.add(darkpool_print_key(ticker, pub) if dataset == "darkpool" else ("i", pub["id"]))
        return out


def stored_keys(catalog, dataset: str, ticker: str, open_utc: datetime, close_utc: datetime) -> set:
    out = set()
    for o in catalog.observations_between(dataset, ticker, open_utc, close_utc):
        out.add(darkpool_print_key(ticker, o) if dataset == "darkpool" else ("i", o.get("id")))
    return out
