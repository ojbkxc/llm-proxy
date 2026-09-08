#!/usr/bin/env python3
"""跨平台 Codex 模型切换启动器。凭据通过环境变量或 .env 提供。"""
import argparse, os, platform, shlex, shutil, subprocess, sys
SCRIPT_DIR=os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS=platform.system()=="Windows"
def _dotenv():
 d={}
 try:
  with open(os.path.join(SCRIPT_DIR,'.env'),encoding='utf-8') as f:
   for line in f:
    if '=' in line and not line.lstrip().startswith('#'):
     k,v=line.split('=',1);d[k.strip()]=v.strip().strip("'\"")
 except OSError: pass
 return d
D=_dotenv()
def env(k,default=''): return os.environ.get(k) or D.get(k) or default
REMOTE_HOST=env('REMOTE_HOST','104.223.65.202'); REMOTE_PORT=int(env('REMOTE_PORT','10122')); REMOTE_USER=env('REMOTE_USER','root'); REMOTE_PASS=env('REMOTE_PASS','')
REMOTE_WS_PORT=int(env('REMOTE_WS_PORT','20130')); REMOTE_MODEL_PY=env('REMOTE_MULTI_MODEL_PY','/opt/Codex/remote-model.py')
PROFILES={'fast':'gpt-5.6-luna-fast','sfast':'gpt-5.6-sol-fast','mid':'gpt-5.6-sol','code':'gpt-5.6-luna','deep':'gpt-6-astra'}
ALIASES={'astra':'gpt-6-astra','sol':'gpt-5.6-sol','luna':'gpt-5.6-luna','sol-fast':'gpt-5.6-sol-fast','luna-fast':'gpt-5.6-luna-fast'}
def resolve_model(name): return PROFILES.get(name,ALIASES.get(name,name or ''))
def ssh_connect():
 try: import paramiko
 except ImportError: raise SystemExit('缺少 paramiko，请先 pip install paramiko')
 client=paramiko.SSHClient();client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
 kwargs={'hostname':REMOTE_HOST,'port':REMOTE_PORT,'username':REMOTE_USER,'timeout':20}
 if REMOTE_PASS: kwargs['password']=REMOTE_PASS
 client.connect(**kwargs);return client
def remote_set_model(model):
 ssh=ssh_connect()
 try:
  _,out,err=ssh.exec_command('python3 '+shlex.quote(REMOTE_MODEL_PY)+' set '+shlex.quote(model),timeout=120);rc=out.channel.recv_exit_status();print(out.read().decode(errors='replace'));print(err.read().decode(errors='replace'),file=sys.stderr);return rc==0
 finally: ssh.close()
def run_local(profile, prompt=None):
 cmd=[shutil.which('Codex') or 'Codex']
 if prompt is None: cmd += ['--profile',profile] if profile in PROFILES else ['-c','model='+resolve_model(profile)]
 else: cmd += ['exec','--sandbox','danger-full-access','-c','model='+resolve_model(profile),prompt]
 return subprocess.run(cmd).returncode
def main():
 p=argparse.ArgumentParser();p.add_argument('profile',nargs='?');p.add_argument('--exec',dest='prompt');p.add_argument('--remote',action='store_true');p.add_argument('--set-only',action='store_true');p.add_argument('--list',action='store_true');a=p.parse_args()
 if a.list or a.profile=='list':
  for k,v in PROFILES.items(): print(k,v)
  return 0
 if not a.profile: p.error('需要模型档位')
 model=resolve_model(a.profile)
 if a.remote:
  if not remote_set_model(model): return 1
  if a.set_only:return 0
  token=env('CODEX_WS_TOKEN');e=dict(os.environ)
  if token:e['CODEX_WS_TOKEN']=token
  return subprocess.run([shutil.which('Codex') or 'Codex','--remote',f'ws://{REMOTE_HOST}:{REMOTE_WS_PORT}'],env=e).returncode
 return run_local(a.profile,a.prompt)
if __name__=='__main__': raise SystemExit(main())
