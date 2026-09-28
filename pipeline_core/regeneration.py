#!/usr/bin/env python3
"""Transactionally regenerate an explicitly public subset of regional layers."""
from __future__ import annotations
import argparse,json,os,shutil,subprocess,sys,tempfile
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import manage

def now():return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
def read(path):return json.loads(path.read_text(encoding="utf-8"))
def command_timeout():
 try:return max(60,min(24*60*60,int(os.environ.get("RMF_REGENERATION_TIMEOUT_SECONDS","3600"))))
 except ValueError:return 3600
def main():
 p=argparse.ArgumentParser();p.add_argument("--region",required=True);p.add_argument("--reserved-lock",action="store_true",help=argparse.SUPPRESS);a=p.parse_args();rid=manage.safe_id(a.region,"region_id");root=manage.region_dir(rid)
 runtime=manage.runtime_dir(root);runtime.mkdir(parents=True,exist_ok=True);state_path=runtime/"regeneration.json";lock=runtime/".regeneration.lock"
 if a.reserved_lock:
  if not lock.is_file():raise SystemExit("Reserved regeneration lock is missing")
 else:
  try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
  except FileExistsError:raise SystemExit("Regeneration is already running")
  with os.fdopen(fd,"w",encoding="ascii") as stream:stream.write(str(os.getpid()))
 state={"schema_version":1,"region_id":rid,"status":"running","started_at":now()}
 try:
  if a.reserved_lock:lock.write_text(str(os.getpid()),encoding="ascii")
  try:previous=read(state_path) if state_path.is_file() else {}
  except json.JSONDecodeError:previous={}
  if previous.get("last_success_at"):state["last_success_at"]=previous["last_success_at"]
  cfg=read(root/"region.json");spec_path=root/"pipeline/regeneration.json";spec=read(spec_path);manage.validate_regeneration(root,cfg)
  if not spec.get("enabled"):raise ValueError("Public regeneration is not enabled")
  cooldown=spec["min_interval_seconds"];timeout=command_timeout();state|={"min_interval_seconds":cooldown,"command_timeout_seconds":timeout};manage.atomic_json(state_path,state)
  data=root/"data";backup=root/".data.regeneration.backup"
  if not data.exists() and backup.is_dir():os.replace(backup,data)
  if data.is_symlink() or not data.is_dir():raise ValueError("Regeneration requires a local data directory, not a symlink")
  outputs=spec.get("outputs",[]);commands=spec.get("commands",[])
  if not isinstance(outputs,list) or not outputs or not all(isinstance(item,str) and item for item in outputs) or len(outputs)!=len(set(outputs)):raise ValueError("Regeneration outputs must be a non-empty unique string list")
  if not commands or not isinstance(commands,list):raise ValueError("Regeneration commands must be a non-empty list")
  allowed={manage.data_relative(layer["file"]):key for key,layer in cfg["layers"].items() if layer.get("regenerable") is True}
  requested=[Path(item) for item in outputs if isinstance(item,str)]
  if len(requested)!=len(outputs) or any(item not in allowed for item in requested):raise ValueError("Every regeneration output must belong to a regenerable layer")
  with tempfile.TemporaryDirectory(prefix=".regeneration-",dir=root) as temporary:
   staged=Path(temporary)/"data";shutil.copytree(data,staged)
   for item in requested:(staged/item).unlink(missing_ok=True)
   env=os.environ|{"RMF_REGION_ROOT":str(root),"RMF_OUTPUT_DIR":str(staged),"RMF_REGENERATION":"1"}
   for step in commands:
    if not isinstance(step,dict) or not isinstance(step.get("cwd","."),str):raise ValueError("Every regeneration command must be an object with a string cwd")
    command=step.get("command",[]);cwd=(root/step.get("cwd",".")).resolve()
    if not isinstance(command,list) or not command or not all(isinstance(item,str) and item for item in command):raise ValueError("Invalid regeneration command")
    if root not in cwd.parents and cwd!=root:raise ValueError("Invalid regeneration working directory")
    subprocess.run([sys.executable if x=="{python}" else x for x in command],cwd=cwd,env=env,check=True,timeout=timeout)
   for item in requested:
    path=staged/item
    if not path.is_file():raise ValueError(f"Regeneration did not create {item}")
    manage.validate_geojson(path,allowed[item],cfg["layers"][allowed[item]])
   for key,layer in cfg["layers"].items():manage.validate_geojson(staged/manage.data_relative(layer["file"]),key,layer)
   backup=root/".data.regeneration.backup";published=False
   if backup.exists():raise RuntimeError(f"Recovery backup already exists: {backup}")
   os.replace(data,backup)
   try:
    os.replace(staged,data);published=True
    manage.write_publication_manifest(root,cfg,{"kind":"public_regeneration","definition":manage.record(spec_path,root),"outputs":[str(x) for x in requested]})
   except BaseException:
    if published and data.exists():shutil.rmtree(data)
    os.replace(backup,data);raise
   shutil.rmtree(backup)
  finished=now();state|={"status":"succeeded","finished_at":finished,"last_success_at":finished,"outputs":[str(x) for x in requested]};manage.atomic_json(state_path,state)
 except BaseException as error:
  state|={"status":"failed","finished_at":now(),"error":str(error)};manage.atomic_json(state_path,state);raise
 finally:lock.unlink(missing_ok=True)
if __name__=="__main__":main()
