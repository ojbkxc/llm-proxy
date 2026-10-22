import os, tempfile, json, threading, urllib.request, urllib.error
os.environ['QODER_PROXY_ACCOUNTS'] = os.path.join(tempfile.mkdtemp(), 'a.json')
os.environ['QODER_PROXY_CHECKIN_STATE'] = os.path.join(tempfile.mkdtemp(), 's.json')
import qoder_proxy as Q
import qoder_admin as QA
tmp = tempfile.mkdtemp()
Q.ACCOUNTS_FILE = os.path.join(tmp, 'accounts.json')
with open(Q.ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
    json.dump({'version': 1, 'accounts': [{'token': 'dt-fake', 'uid': 'fake-uid', 'machineId': 'm1', 'account': 'fake', 'exp': 9999999999, 'realm': 'cn'}]}, f)
Q.ADMIN_KEYS_FILE = os.path.join(tmp, 'keys.json')
Q.MODEL_ROUTES_FILE = os.path.join(tmp, 'routes.json')
Q.ADMIN_PASSWORD = ''
Q._key_store = QA.KeyStore(Q.ADMIN_KEYS_FILE)
Q._pool.update({'accounts': [], 'mtime': 0.0})
Q._models.update({'at': 0.0, 'list': [], 'by_id': {}, 'alias': {}})
Q._model_routes.update({'routes': {'mappings': {}, 'aliases': {}}, 'mtime': 0.0})
with Q._usage_stats_lock:
    Q._usage_stats.clear()
srv = Q.QuietServer(('127.0.0.1', 0), Q.Handler)
port = srv.server_address[1]
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()
try:
    r = urllib.request.urlopen('http://127.0.0.1:%d/admin' % port, timeout=5)
    print('admin page:', r.status)
except urllib.error.HTTPError as e:
    print('admin page HTTPError:', e.code, e.read()[:300])
except Exception as e:
    print('admin page exc:', repr(e))
try:
    r = urllib.request.urlopen('http://127.0.0.1:%d/api/keys' % port, timeout=5)
    print('keys:', r.status)
except urllib.error.HTTPError as e:
    print('keys HTTPError:', e.code, e.read()[:300])
except Exception as e:
    print('keys exc:', repr(e))
srv.shutdown()