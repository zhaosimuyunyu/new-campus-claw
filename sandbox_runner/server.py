"""Narrow execution broker. No user-controlled Docker options or host mounts."""
import base64
import json
import os
import re
import secrets
import threading
import time
from pathlib import PurePosixPath
import docker
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI()
slots = threading.BoundedSemaphore(2)

BOOTSTRAP = r'''
import sys,json,pathlib,subprocess,base64
d=json.loads(sys.stdin.readline())
root=pathlib.Path('/tmp/workspace');root.mkdir()
for name,body in d.get('binary_files',{}).items():
    target=root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(base64.b64decode(body,validate=True))
for name,body in d['files'].items():
    target=root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(body)
status=subprocess.call(d['command'],cwd=root)
outputs=[];total=0
folder=root/'outputs'
if folder.exists():
    for path in sorted(folder.rglob('*')):
        if len(outputs)>=8:break
        if not path.is_file() or not path.resolve().is_relative_to(root):continue
        if path.suffix.lower() not in {'.png','.jpg','.jpeg','.pdf','.docx','.csv','.txt','.json','.html','.md','.ipynb'}:continue
        if path.stat().st_size+total>900000:continue
        raw=path.read_bytes();total+=len(raw)
        outputs.append({'name':path.name,'base64':base64.b64encode(raw).decode()})
print('\n__CAMPUS_OUTPUT_FILES__'+json.dumps(outputs))
sys.exit(status)
'''


class Execution(BaseModel):
    files: dict[str, str]
    binary_files: dict[str, str] = Field(default_factory=dict)
    entrypoint: str
    args: list[str] = Field(default_factory=list)
    browser_url: str | None = None


BROWSER_SCRIPT = r'''
import json,sys,socket,ipaddress,base64
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

def check(url):
    p=urlsplit(url)
    if p.scheme not in ("http","https") or not p.hostname or p.username or p.password:
        return False
    try:
        return all(ipaddress.ip_address(r[4][0]).is_global for r in socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80),type=socket.SOCK_STREAM))
    except Exception:
        return False
url=sys.argv[1]
if not check(url):
    raise ValueError("仅允许访问公网 HTTP(S) 网页")
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--renderer-process-limit=2'])
    page=browser.new_page(viewport={"width":1280,"height":900},accept_downloads=False,service_workers='block')
    page.route('**/*',lambda route: route.continue_() if check(route.request.url) else route.abort())
    page.goto(url,wait_until='domcontentloaded',timeout=20000)
    page.wait_for_timeout(800)
    print(json.dumps({"title":page.title(),"url":page.url,"text":page.locator('body').inner_text()[:24000],"links":page.locator('a[href]').evaluate_all('(els)=>els.slice(0,30).map(e=>({text:e.innerText,url:e.href}))'),"screenshot_base64":base64.b64encode(page.screenshot(type='png')).decode()}))
    browser.close()
'''


@app.post('/execute')
def execute(data: Execution, authorization: str = Header(default='')):
    token = os.getenv('SANDBOX_TOKEN', '')
    if not token or not secrets.compare_digest(authorization, 'Bearer '+token):
        raise HTTPException(401, 'Unauthorized')
    if not slots.acquire(blocking=False):
        raise HTTPException(429, '执行环境忙，请稍后重试')
    container = None
    started = time.monotonic()
    try:
        files = dict(data.files)
        if len(files) + len(data.binary_files)>512 or sum(len(v.encode()) for v in files.values()) + sum(len(v) for v in data.binary_files.values())>8*1024*1024 or len(data.args)>20 or any(len(a)>4000 for a in data.args):
            raise HTTPException(400, '执行输入超出限制')
        if data.browser_url:
            files = {'browser.py': BROWSER_SCRIPT}
            entry, args = 'browser.py', [data.browser_url]
            network_disabled = False
        else:
            entry, args, network_disabled = data.entrypoint, data.args, True
        if set(files) & set(data.binary_files):
            raise HTTPException(400, '文本与二进制资源路径冲突')
        for name in [*files, *data.binary_files]:
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
                raise HTTPException(400, '不安全文件路径')
        if entry not in files:
            raise HTTPException(400, '入口脚本不存在')
        needs_browser = bool(data.browser_url) or any('playwright' in code for code in files.values())
        interpreters={'.py':['python'],'.js':['node'],'.sh':['/bin/sh']}
        suffix=PurePosixPath(entry).suffix
        if suffix not in interpreters:
            raise HTTPException(400, '支持 Python、JavaScript 与 Shell 脚本')
        client=docker.from_env()
        container=client.containers.create('campusclaw-sandbox:local',command=['python','-u','-c',BOOTSTRAP],
            working_dir='/tmp',stdin_open=True,network_disabled=network_disabled,read_only=True,user='65534:65534',
            cap_drop=['ALL'],security_opt=['no-new-privileges:true'],mem_limit='1g' if needs_browser else '512m',nano_cpus=2_000_000_000 if needs_browser else 1_000_000_000,
            pids_limit=512 if needs_browser else 128,shm_size='256m',tmpfs={'/tmp':'rw,noexec,nosuid,size=128m,mode=1777'},
            log_config=docker.types.LogConfig(type='json-file',config={'max-size':'2m','max-file':'1'}),
            labels={'campusclaw':'sandbox'},environment={'HOME':'/tmp','PYTHONDONTWRITEBYTECODE':'1'})
        attached=client.api.attach_socket(container.id,params={'stdin':1,'stream':1})
        container.start()
        attached._sock.sendall((json.dumps({'files':files,'binary_files':{} if data.browser_url else data.binary_files,'command':interpreters[suffix]+[entry]+args})+'\n').encode())
        try:
            result=container.wait(timeout=35)
        except Exception:
            container.kill()
            raise HTTPException(408, '执行超过 35 秒，已终止')
        attached.close()
        stdout=container.logs(stdout=True,stderr=False,tail=200).decode('utf-8','replace')[:1500000]
        stderr=container.logs(stdout=False,stderr=True,tail=100).decode('utf-8','replace')[:10000]
        files=[]
        if '\n__CAMPUS_OUTPUT_FILES__' in stdout:
            stdout,encoded=stdout.rsplit('\n__CAMPUS_OUTPUT_FILES__',1)
            try:files=json.loads(encoded)
            except ValueError:pass
        return {'exit_code':result['StatusCode'],'stdout':stdout[:1500000 if data.browser_url else 30000],'stderr':stderr,'files':files,'duration_ms':round((time.monotonic()-started)*1000), 'network':'public-web' if data.browser_url else 'disabled'}
    finally:
        if container:
            container.remove(force=True)
        slots.release()
