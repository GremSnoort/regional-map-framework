#!/usr/bin/env python3
'Authenticated static map server with an opt-in regeneration API.'
from __future__ import annotations
import argparse,html,ipaddress,json,os,re,subprocess,sys,threading,time
from datetime import datetime,timezone
from http import HTTPStatus
from http.cookies import CookieError,SimpleCookie
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs,quote,unquote,urlparse,urlsplit
import auth
from pipeline_core import contacts

ROOT=Path(__file__).resolve().parent;COOKIE_NAME="rmf_session";LOGIN_LIMIT=5;LOGIN_WINDOW_SECONDS=15*60
FAILURES={};FAILURES_LOCK=threading.Lock()
def content_root():
 value=os.environ.get("RMF_CONTENT_ROOT")
 return Path(value).expanduser().resolve() if value else ROOT
def runtime_dir(region_root):
 value=os.environ.get("RMF_RUNTIME_ROOT")
 return Path(value).expanduser().resolve()/region_root.name if value else region_root/".runtime"
def payload(path,default):
 try:
  value=json.loads(path.read_text(encoding="utf-8"));return value if isinstance(value,dict) else default
 except (FileNotFoundError,json.JSONDecodeError):return default
def interval(spec):
 try:return max(300,int(spec.get("min_interval_seconds",300)))
 except (TypeError,ValueError):return 300
def elapsed_since(value):
 try:return (datetime.now(timezone.utc)-datetime.fromisoformat(value).astimezone(timezone.utc)).total_seconds()
 except (TypeError,ValueError):return None
def process_alive(pid):
 try:os.kill(pid,0);return True
 except (OSError,ValueError):return False
def stale_lock(lock):
 try:return not process_alive(int(lock.read_text(encoding="ascii").strip()))
 except (OSError,ValueError):return False
def reserve_lock(lock):
 lock.parent.mkdir(parents=True,exist_ok=True)
 for attempt in range(2):
  try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
  except FileExistsError:
   if attempt==0 and stale_lock(lock):
    try:lock.unlink()
    except FileNotFoundError:pass
    continue
   return False
  with os.fdopen(fd,"w",encoding="ascii") as stream:stream.write(str(os.getpid()))
  return True
 return False
def write_payload(path,value):
 path.parent.mkdir(exist_ok=True);temporary=path.with_name(f".{path.name}.{os.getpid()}.tmp")
 try:temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8");os.replace(temporary,path)
 finally:temporary.unlink(missing_ok=True)
def safe_next(value):
 if not isinstance(value,str) or not value or len(value)>2048:return "/"
 decoded=value
 try:
  for _ in range(len(value)+1):
   expanded=unquote(decoded,errors="strict")
   if expanded==decoded:break
   decoded=expanded
  parts=urlsplit(decoded)
 except (UnicodeDecodeError,UnicodeEncodeError,ValueError):return "/"
 if not decoded.startswith("/") or decoded.startswith("//") or parts.scheme or parts.netloc or "\\" in decoded or any(ord(char)<32 or ord(char)==127 for char in decoded):return "/"
 return value
def loopback_bind(value):
 if value.lower()=="localhost":return True
 try:return ipaddress.ip_address(value).is_loopback
 except ValueError:return False
def client_ip(handler):
 value=handler.client_address[0]
 if os.environ.get("RMF_TRUST_PROXY")=="1":value=handler.headers.get("X-Forwarded-For",value).split(",",1)[0].strip()
 try:return str(ipaddress.ip_address(value))
 except ValueError:return handler.client_address[0]
def login_limited(key):
 cutoff=time.monotonic()-LOGIN_WINDOW_SECONDS
 with FAILURES_LOCK:
  recent=[item for item in FAILURES.get(key,[]) if item>=cutoff];FAILURES[key]=recent;return len(recent)>=LOGIN_LIMIT
def login_failed(key):
 with FAILURES_LOCK:FAILURES.setdefault(key,[]).append(time.monotonic())
def login_succeeded(key):
 with FAILURES_LOCK:FAILURES.pop(key,None)
def same_origin(handler):
 if handler.headers.get("Sec-Fetch-Site","").lower()=="cross-site":return False
 origin=handler.headers.get("Origin")
 return not origin or urlparse(origin).netloc==handler.headers.get("Host","")
def admin_users():
 return {item.strip() for item in os.environ.get("RMF_ADMIN_USERS", "").split(",") if item.strip()}
def is_admin(username):return bool(username) and username in admin_users()
def registered_regions():
 value=payload(content_root()/"registry.json",{})
 regions=value.get("regions",[])
 if not isinstance(regions,list):return set()
 return {item for item in regions if isinstance(item,str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*",item)}
def beneath(path,root):
 try:path.relative_to(root);return True
 except ValueError:return False
def public_file(request_path):
 """Resolve only explicitly public application files; return None for everything else."""
 try:path=unquote(request_path,errors="strict")
 except (UnicodeDecodeError,UnicodeEncodeError):return None
 if "\0" in path or "\\" in path or "//" in path:return None
 fixed={"/":"index.html","/index.html":"index.html","/map.html":"map.html","/contacts.html":"contacts.html","/core/gallery.js":"core/gallery.js","/core/gallery.css":"core/gallery.css","/core/map.js":"core/map.js","/core/map.css":"core/map.css","/core/contacts.js":"core/contacts.js","/core/contacts.css":"core/contacts.css"}
 if path=="/registry.json":
  root=content_root();candidate=root/"registry.json"
  try:resolved=candidate.resolve(strict=True);safe_root=root.resolve(strict=True)
  except OSError:return None
  return resolved if candidate.is_file() and not candidate.is_symlink() and beneath(resolved,safe_root) else None
 if path in fixed:
  candidate=ROOT/fixed[path]
  try:resolved=candidate.resolve(strict=True);safe_root=ROOT.resolve(strict=True)
  except OSError:return None
  return resolved if candidate.is_file() and not candidate.is_symlink() and beneath(resolved,safe_root) else None
 match=re.fullmatch(r"/regions/([a-z0-9][a-z0-9_-]*)/(region\.json|data/.+)",path)
 if not match:return None
 region_id,relative=match.groups()
 if region_id not in registered_regions():return None
 region_root=content_root()/"regions"/region_id
 if region_root.is_symlink() or not region_root.is_dir():return None
 config_path=region_root/"region.json"
 if config_path.is_symlink():return None
 try:
  safe_region=region_root.resolve(strict=True);config_resolved=config_path.resolve(strict=True)
 except OSError:return None
 if not beneath(config_resolved,safe_region):return None
 if relative=="region.json":return config_resolved if config_resolved.is_file() else None
 config=payload(config_path,{})
 layers=config.get("layers",{})
 if not isinstance(layers,dict):return None
 declared={spec.get("file") for spec in layers.values() if isinstance(spec,dict) and isinstance(spec.get("file"),str)}
 if relative not in declared:return None
 try:
  data_root=(region_root/"data").resolve(strict=True)
  candidate=(region_root/relative).resolve(strict=True)
 except OSError:return None
 return candidate if candidate.is_file() and beneath(candidate,data_root) else None
def login_page(next_url,error=""):
 message=f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
 return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход — региональные аналитические карты</title><style>html,body{{height:100%;margin:0}}body{{display:grid;place-items:center;background:#edf3ef;font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;color:#17211b}}main{{width:min(390px,calc(100vw - 48px));padding:30px;background:#fffffff7;border:1px solid #cbd7d0;border-radius:14px;box-shadow:0 12px 42px #17352320}}h1{{margin:0 0 8px;font-size:24px}}p{{color:#66716b;font-size:13px;line-height:1.45}}label{{display:block;margin-top:16px;font-size:13px;font-weight:650}}input{{box-sizing:border-box;width:100%;margin-top:6px;padding:11px 12px;border:1px solid #aebdb5;border-radius:8px;font:inherit}}button{{width:100%;margin-top:20px;padding:11px;border:0;border-radius:8px;background:#176636;color:white;font:inherit;font-weight:700;cursor:pointer}}.error{{padding:9px 11px;border-radius:7px;background:#fff0f0;color:#a31212}}</style></head>
<body><main><h1>Вход в карты</h1><p>Доступ выдаёт администратор. Самостоятельной регистрации и восстановления пароля нет.</p>{message}<form method="post" action="/auth/login"><input type="hidden" name="next" value="{html.escape(next_url,quote=True)}"><label>Логин<input name="username" autocomplete="username" required autofocus maxlength="64"></label><label>Пароль<input name="password" type="password" autocomplete="current-password" required maxlength="1024"></label><button type="submit">Войти</button></form></main></body></html>'''.encode("utf-8")

class Handler(SimpleHTTPRequestHandler):
 server_version="RegionalMapFramework";sys_version=""
 def end_headers(self):
  self.send_header("Cache-Control","private, no-store");self.send_header("X-Content-Type-Options","nosniff");self.send_header("Referrer-Policy","strict-origin-when-cross-origin");self.send_header("X-Frame-Options","DENY");self.send_header("Permissions-Policy","camera=(), microphone=(), geolocation=()");self.send_header("Cross-Origin-Resource-Policy","same-origin");self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self' https://unpkg.com 'sha256-VwxUbM8Aa6fXOFgyj+XuKWSCSgp5VLJbBskEKbQGEKw='; style-src 'self' 'unsafe-inline' https://unpkg.com; img-src 'self' data: blob: https://tiles.openfreemap.org https://unpkg.com; connect-src 'self' https://tiles.openfreemap.org https://unpkg.com; worker-src 'self' blob: https://unpkg.com; child-src 'self' blob: https://unpkg.com; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'");super().end_headers()
 def reply(self,code,value,head_only=False):
  body=json.dumps(value,ensure_ascii=False).encode();self.send_response(code);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(body)));self.end_headers()
  if not head_only:self.wfile.write(body)
 def html(self,code,body):
  self.send_response(code);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
 def redirect(self,target,cookie=None):
  self.send_response(HTTPStatus.SEE_OTHER);self.send_header("Location",target)
  if cookie:self.send_header("Set-Cookie",cookie)
  self.end_headers()
 def session_token(self):
  try:cookie=SimpleCookie();cookie.load(self.headers.get("Cookie",""));morsel=cookie.get(COOKIE_NAME);return morsel.value if morsel else ""
  except (CookieError,ValueError):return ""
 def username(self):
  if not hasattr(self,"_username"):self._username=auth.verify_session(self.session_token())
  return self._username
 def require_login(self,api=False):
  if self.username():return True
  if api:self.reply(HTTPStatus.UNAUTHORIZED,{"error":"authentication required"})
  else:self.redirect(f"/login?next={quote(safe_next(self.path),safe='')}")
  return False
 def api_region(self):
  rid=parse_qs(urlparse(self.path).query).get("region",[""])[0]
  if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*",rid) or rid not in registered_regions():return None,None
  root=content_root()/"regions"/rid
  if root.is_symlink() or not root.is_dir():return None,None
  return root,runtime_dir(root)/"regeneration.json"
 def read_form(self):
  try:length=int(self.headers.get("Content-Length","0"))
  except ValueError:length=0
  if length<0 or length>4096:raise ValueError("Invalid form size")
  if self.headers.get("Content-Type","").split(";",1)[0].strip().lower()!="application/x-www-form-urlencoded":raise ValueError("Unsupported form encoding")
  parsed=parse_qs(self.rfile.read(length).decode("utf-8"),keep_blank_values=True);return {key:values[0] for key,values in parsed.items() if values}
 def serve_public_file(self,head_only=False):
  path=public_file(urlparse(self.path).path)
  if path is None:return self.send_error(HTTPStatus.NOT_FOUND,"Not found")
  try:
   stream=path.open("rb");metadata=os.fstat(stream.fileno())
  except OSError:return self.send_error(HTTPStatus.NOT_FOUND,"Not found")
  try:
   self.send_response(HTTPStatus.OK);self.send_header("Content-Type",self.guess_type(str(path)));self.send_header("Content-Length",str(metadata.st_size));self.send_header("Last-Modified",self.date_time_string(metadata.st_mtime));self.end_headers()
   if not head_only:self.copyfile(stream,self.wfile)
  finally:stream.close()
 def do_HEAD(self):
  if urlparse(self.path).path=="/healthz":return self.reply(HTTPStatus.OK,{"status":"ok"},head_only=True)
  if self.require_login():self.serve_public_file(head_only=True)
 def do_GET(self):
  path=urlparse(self.path).path
  if path=="/healthz":return self.reply(HTTPStatus.OK,{"status":"ok"})
  if path=="/login":
   next_url=safe_next(parse_qs(urlparse(self.path).query).get("next",["/"])[0])
   if self.username():return self.redirect(next_url)
   return self.html(HTTPStatus.OK,login_page(next_url))
  if not self.require_login(api=path.startswith("/api/")):return
  if path=="/api/session":return self.reply(HTTPStatus.OK,{"username":self.username(),"is_admin":is_admin(self.username())})
  if path in {"/api/contacts","/api/contacts/status","/api/contacts/candidates"}:
   root,_=self.api_region()
   if not root:return self.reply(HTTPStatus.BAD_REQUEST,{"error":"invalid region"})
   try:spec=contacts.manage.validate_contacts(root,contacts.manage.read_json(root/"region.json"))
   except (OSError,ValueError,json.JSONDecodeError) as error:return self.reply(HTTPStatus.INTERNAL_SERVER_ERROR,{"error":str(error)})
   if not spec or not spec.get("enabled"):return self.reply(HTTPStatus.NOT_FOUND,{"enabled":False})
   target=contacts.paths(root)
   if path=="/api/contacts":
    try:value=contacts.validate_catalog(contacts.manage.read_json(target["published"]),root.name,allow_candidates=False) if target["published"].is_file() else {"schema_version":1,"region_id":root.name,"contacts":[]}
    except (OSError,ValueError,json.JSONDecodeError) as error:return self.reply(HTTPStatus.INTERNAL_SERVER_ERROR,{"error":f"published contact catalog is invalid: {error}"})
    value=dict(value);value.pop("published_by",None);value["enabled"]=True;return self.reply(HTTPStatus.OK,value)
   if not is_admin(self.username()):return self.reply(HTTPStatus.FORBIDDEN,{"error":"administrator access required"})
   if path=="/api/contacts/candidates":
    if not target["candidates"].is_file():return self.reply(HTTPStatus.NOT_FOUND,{"error":"candidate catalog is missing"})
    return self.reply(HTTPStatus.OK,payload(target["candidates"],{}))
   value=payload(target["state"],{"status":"never"});value|={"enabled":True,"min_interval_seconds":max(3600,int(spec["min_interval_seconds"])),"has_candidates":target["candidates"].is_file(),"has_publication":target["published"].is_file()};return self.reply(HTTPStatus.OK,value)
  if path=="/api/regeneration":
   root,state=self.api_region()
   if not root or not (root/"pipeline/regeneration.json").is_file():return self.reply(HTTPStatus.NOT_FOUND,{"enabled":False})
   spec=payload(root/"pipeline/regeneration.json",{});value=payload(state,{"status":"never"});value|={"enabled":bool(spec.get("enabled")),"min_interval_seconds":interval(spec)};return self.reply(HTTPStatus.OK,value)
  return self.serve_public_file()
 def do_POST(self):
  path=urlparse(self.path).path
  if not same_origin(self):return self.reply(HTTPStatus.FORBIDDEN,{"error":"cross-site request rejected"})
  if path=="/auth/login":
   try:form=self.read_form()
   except (ValueError,UnicodeDecodeError):return self.reply(HTTPStatus.BAD_REQUEST,{"error":"invalid login request"})
   username=form.get("username","");password=form.get("password","");next_url=safe_next(form.get("next"));key=client_ip(self)
   if login_limited(key):return self.html(HTTPStatus.TOO_MANY_REQUESTS,login_page(next_url,"Слишком много попыток. Повторите позднее."))
   if not auth.verify_password(username,password):login_failed(key);return self.html(HTTPStatus.UNAUTHORIZED,login_page(next_url,"Неверный логин или пароль."))
   login_succeeded(key);token=auth.create_session(username);attributes=[f"{COOKIE_NAME}={token}","Path=/","HttpOnly","SameSite=Strict",f"Max-Age={auth.session_seconds()}"]
   if os.environ.get("RMF_COOKIE_SECURE")=="1":attributes.append("Secure")
   return self.redirect(next_url,"; ".join(attributes))
  if path=="/auth/logout":return self.redirect("/login",f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
  if not self.require_login(api=True):return
  if path in {"/api/contacts/collect","/api/contacts/publish"}:
   if not is_admin(self.username()):return self.reply(HTTPStatus.FORBIDDEN,{"error":"administrator access required"})
   if os.environ.get("RMF_ALLOW_CONTACT_COLLECTION")!="1":return self.reply(HTTPStatus.FORBIDDEN,{"error":"contact collection and publication are disabled"})
   root,_=self.api_region()
   if not root:return self.reply(HTTPStatus.BAD_REQUEST,{"error":"invalid region"})
   try:spec=contacts.manage.validate_contacts(root,contacts.manage.read_json(root/"region.json"))
   except (OSError,ValueError,json.JSONDecodeError) as error:return self.reply(HTTPStatus.BAD_REQUEST,{"error":str(error)})
   if not spec or not spec.get("enabled"):return self.reply(HTTPStatus.NOT_FOUND,{"error":"contact catalog is not enabled"})
   target=contacts.paths(root)
   if path=="/api/contacts/publish":
    try:value=contacts.publish(root.name,self.username())
    except (OSError,ValueError,json.JSONDecodeError) as error:return self.reply(HTTPStatus.CONFLICT,{"error":str(error)})
    return self.reply(HTTPStatus.OK,{"status":"published","published_at":value["published_at"],"count":len(value["contacts"])})
   state=payload(target["state"],{});cooldown=max(3600,int(spec["min_interval_seconds"]));elapsed=elapsed_since(state.get("started_at"))
   if elapsed is not None and elapsed<cooldown:return self.reply(HTTPStatus.TOO_MANY_REQUESTS,{"error":"cooldown","retry_after_seconds":max(1,round(cooldown-elapsed))})
   if not reserve_lock(target["lock"]):return self.reply(HTTPStatus.CONFLICT,{"error":"already running"})
   started=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds");running={"schema_version":1,"region_id":root.name,"status":"running","started_at":started,"min_interval_seconds":cooldown}
   if state.get("published_at"):running["published_at"]=state["published_at"]
   write_payload(target["state"],running)
   try:
    log_path=target["base"]/"collection.log";fd=os.open(log_path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    if os.name!="nt":os.chmod(log_path,0o600)
    with os.fdopen(fd,"wb") as log:process=subprocess.Popen([sys.executable,str(ROOT/"pipeline_core/contacts.py"),"--region",root.name,"--reserved-lock"],cwd=ROOT,start_new_session=True,stdout=log,stderr=subprocess.STDOUT)
    target["lock"].write_text(str(process.pid),encoding="ascii")
   except BaseException as error:
    running|={"status":"failed","finished_at":datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),"error":str(error)};write_payload(target["state"],running);target["lock"].unlink(missing_ok=True);return self.reply(HTTPStatus.INTERNAL_SERVER_ERROR,{"error":"failed to start contact collection"})
   return self.reply(HTTPStatus.ACCEPTED,{"status":"accepted","region_id":root.name})
  if path!="/api/regeneration":return self.reply(HTTPStatus.NOT_FOUND,{"error":"not found"})
  root,state_path=self.api_region()
  if not root:return self.reply(HTTPStatus.BAD_REQUEST,{"error":"invalid region"})
  if os.environ.get("RMF_ALLOW_REGENERATION")!="1":return self.reply(HTTPStatus.FORBIDDEN,{"error":"server-side regeneration is disabled"})
  spec=payload(root/"pipeline/regeneration.json",{});state=payload(state_path,{})
  if not spec.get("enabled"):return self.reply(HTTPStatus.NOT_FOUND,{"error":"not enabled"})
  cooldown=interval(spec);elapsed=elapsed_since(state.get("started_at"))
  if elapsed is not None and elapsed<cooldown:return self.reply(HTTPStatus.TOO_MANY_REQUESTS,{"error":"cooldown","retry_after_seconds":max(1,round(cooldown-elapsed))})
  lock=runtime_dir(root)/".regeneration.lock"
  if not reserve_lock(lock):return self.reply(HTTPStatus.CONFLICT,{"error":"already running"})
  started=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds");running={"schema_version":1,"region_id":root.name,"status":"running","started_at":started,"min_interval_seconds":cooldown,"log_file":".runtime/regeneration.log"}
  if state.get("last_success_at"):running["last_success_at"]=state["last_success_at"]
  write_payload(state_path,running)
  try:
   log_path=runtime_dir(root)/"regeneration.log";fd=os.open(log_path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
   if os.name!="nt":os.chmod(log_path,0o600)
   with os.fdopen(fd,"wb") as log:
    process=subprocess.Popen([sys.executable,str(ROOT/"pipeline_core/regeneration.py"),"--region",root.name,"--reserved-lock"],cwd=ROOT,start_new_session=True,stdout=log,stderr=subprocess.STDOUT)
   lock.write_text(str(process.pid),encoding="ascii")
  except BaseException as error:
   running|={"status":"failed","finished_at":datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),"error":str(error)};write_payload(state_path,running);lock.unlink(missing_ok=True);return self.reply(HTTPStatus.INTERNAL_SERVER_ERROR,{"error":"failed to start regeneration"})
  return self.reply(HTTPStatus.ACCEPTED,{"status":"accepted","region_id":root.name})
 def log_message(self,format,*args):super().log_message(format,*args)
def main():
 parser=argparse.ArgumentParser();parser.add_argument("--bind",default="127.0.0.1");parser.add_argument("--port",type=int,default=8000);args=parser.parse_args();store=auth.load_store()
 if os.environ.get("RMF_CONTENT_ROOT") and not (content_root()/"registry.json").is_file():raise SystemExit(f"Content registry is missing: {content_root()/'registry.json'}")
 if not store["users"]:raise SystemExit("Credential store has no users. Run: python3 manage.py auth-set-user <username>")
 if not loopback_bind(args.bind) and os.environ.get("RMF_COOKIE_SECURE")!="1" and os.environ.get("RMF_ALLOW_INSECURE_HTTP")!="1":raise SystemExit("Refusing a non-loopback bind without secure cookies. Use HTTPS + RMF_COOKIE_SECURE=1, or RMF_ALLOW_INSECURE_HTTP=1 only for isolated development")
 os.chdir(ROOT);ThreadingHTTPServer((args.bind,args.port),Handler).serve_forever()
if __name__=="__main__":main()
