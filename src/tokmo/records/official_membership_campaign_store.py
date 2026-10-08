"""Durable cumulative start accounting for both official-membership gates."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import os, sqlite3
from tokmo.records.durability import fsync_directory
from tokmo.records.private_sqlite import (MIN_SQLITE_VERSION, acquire_immediate_transaction, assert_no_sidecars_after_close, assert_private_sidecars_while_open, connect_private, prepare_private_path, private_creation, table_is_strict)

CAMPAIGN_FAMILY="official-membership-live-transition-v1"; CAMPAIGN_CEILING=32
_ENDPOINTS={"SEC_SIC","NASDAQ_LISTED","OTHER_LISTED","SEC_REGISTRY","SEC_SUBMISSIONS"}; _TERMINAL={"SUCCEEDED","FAILED","UNKNOWN"}
_DDL="CREATE TABLE starts(ordinal INTEGER PRIMARY KEY, family TEXT NOT NULL CHECK(family='official-membership-live-transition-v1'), endpoint_kind TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('STARTED','TERMINAL')), started_at TEXT NOT NULL, terminal_code TEXT NOT NULL) STRICT"

@dataclass(frozen=True,slots=True)
class CampaignReservationV1: ordinal:int; endpoint_kind:str; status:str; started_at:datetime

class OfficialMembershipCampaignStore:
 def __init__(self,path:Path,*,clock): self.path=Path(path); self._clock=clock
 def _open(self,*,readonly=False):
  if (sqlite3.sqlite_version_info<MIN_SQLITE_VERSION
      or prepare_private_path(self.path).unsafe
      or (self.path.exists() and os.lstat(self.path).st_nlink != 1)):
   raise ValueError("private_path")
  new=not os.path.lexists(self.path)
  try:
   if new:
    with private_creation(): c=connect_private(self.path)
    os.chmod(self.path,0o600); c.execute(_DDL); c.execute("PRAGMA user_version=1"); assert_private_sidecars_while_open(self.path); fsync_directory(self.path.parent)
   else: c=connect_private(self.path,mode="ro" if readonly else "rwc")
   if c.execute("PRAGMA user_version").fetchone()!=(1,) or not table_is_strict(c,"starts") or c.execute("SELECT sql FROM sqlite_master WHERE name='starts'").fetchone()!= (_DDL,): raise ValueError("schema_mismatch")
   return c
  except BaseException:
   if 'c' in locals(): c.close()
   raise
 def _close(self,c,*,write=False):
  c.close()
  if write: fsync_directory(self.path.parent)
  if assert_no_sidecars_after_close(self.path): raise ValueError("sqlite_sidecar_present")
 def reserve(self,endpoint_kind):
  if endpoint_kind not in _ENDPOINTS: raise ValueError("endpoint_kind")
  c=self._open(); stamp=self._clock()
  try:
   acquire_immediate_transaction(c); used=c.execute("SELECT count(*) FROM starts").fetchone()[0]
   if used>=CAMPAIGN_CEILING: c.execute("ROLLBACK"); raise ValueError("campaign_ceiling")
   ordinal=used+1; c.execute("INSERT INTO starts VALUES(?,?,?,?,?,?)",(ordinal,CAMPAIGN_FAMILY,endpoint_kind,"STARTED",stamp.isoformat(),"")); c.execute("COMMIT")
  finally: self._close(c,write=True)
  return CampaignReservationV1(ordinal,endpoint_kind,"STARTED",stamp)
 def finish(self,ordinal,terminal_code):
  if terminal_code not in _TERMINAL: raise ValueError("terminal_code")
  c=self._open()
  try:
   acquire_immediate_transaction(c); changed=c.execute("UPDATE starts SET status='TERMINAL',terminal_code=? WHERE ordinal=? AND status='STARTED'",(terminal_code,ordinal)).rowcount
   if changed!=1: c.execute("ROLLBACK"); raise ValueError("reservation")
   c.execute("COMMIT")
  finally:self._close(c,write=True)
 def usage(self):
  c=self._open(readonly=True)
  try:return c.execute("SELECT count(*) FROM starts").fetchone()[0]
  finally:self._close(c)
 def read(self,ordinal):
  c=self._open(readonly=True)
  try:row=c.execute("SELECT endpoint_kind,status,started_at,terminal_code FROM starts WHERE ordinal=?",(ordinal,)).fetchone()
  finally:self._close(c)
  if row is None: raise ValueError("reservation")
  return CampaignReservationV1(ordinal,row[0],row[3] if row[1]=="TERMINAL" else row[1],datetime.fromisoformat(row[2]))
