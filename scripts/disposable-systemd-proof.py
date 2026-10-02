#!/usr/bin/env python3
"""Disposable GitHub VM only: runtime evidence, never a deployment installer."""
import json, os, pathlib, re, socket, sqlite3, subprocess, threading, time
P = pathlib.Path

def run(*args, check=True):
    r = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print('$', ' '.join(args), '\n', r.stdout, flush=True)
    if check and r.returncode: raise RuntimeError((args, r.returncode))
    return r

def put(path, text, mode=0o644):
    P(path).write_text(text); os.chmod(path, mode)

assert os.geteuid() == 0 and os.environ.get('GITHUB_ACTIONS') == 'true'
print('PID1:', P('/proc/1/comm').read_text(), flush=True)
assert P('/proc/1/comm').read_text().strip() == 'systemd', 'Runner PID1 is not systemd'
run('systemctl', 'is-system-running', check=False)
doc = P('INSTALL.md').read_text()
# Execute the documented account/directory block verbatim; only this VM is changed.
block = re.search(r'```bash\n(sudo groupadd --system cncli-db.*?)```', doc, re.S).group(1)
run('bash', '-ec', block)
run('groupadd', '--system', 'cncli-fixture-socket')
run('install', '-d', '-o', 'root', '-g', 'cncli-fixture-socket', '-m', '0750', '/run/cardano-node')
for unit in ('cncli-sync.service', 'cncli-sendtip.service', 'cncli-leaderlog.service', 'cncli-leaderlog.timer'):
    text = re.search(r'`'+re.escape(unit)+r'`[^\n]*:\n\n```ini\n(.*?)```', doc, re.S).group(1)
    put('/etc/systemd/system/'+unit, text.replace('OPERATOR_SOCKET_GROUP', 'cncli-fixture-socket'))
put('/etc/cncli/pooltool.json', json.dumps({'api_key':'SYNTHETIC_NOT_A_SECRET', 'pools':[{'name':'fixture','pool_id':'01'*28,'host':'127.0.0.1','port':3000}]}), 0o640)
run('chown', 'root:cncli-pooltool', '/etc/cncli/pooltool.json')
put('/etc/cncli/vrf.skey', json.dumps({'type':'VrfSigningKey_PraosVRF','cborHex':'5820'+'01'*32}), 0o640)
run('chown', 'root:cncli-leaderlog', '/etc/cncli/vrf.skey')
for name in ('byron', 'shelley'): put('/etc/cncli/mainnet-'+name+'-genesis.json', '{}')
put('/usr/local/bin/cardano-node', "#!/bin/sh\nprintf 'cardano-node 10.1.0 linux\\ngit rev abcdef012345\\n'\n", 0o755)
put('/usr/local/bin/cardano-cli', '''#!/bin/sh
if [ "$1" = --version ]; then echo 'cardano-cli 1.0.0 fixture'; else
printf '{\n    "poolStakeSet": 100,\n    "activeStakeSet": 1000\n}\n'
fi
''', 0o755)
# This is an explicitly synthetic external command, not candidate leaderlog success.
put('/usr/local/bin/cncli-fixture', '''#!/bin/sh
case "$1" in
--version) echo synthetic-cncli-helper;;
leaderlog) echo '{"status":"ok","epochSlots":1,"assignedSlots":[{"at":"fixture","slot":42,"no":1}]}' ;;
*) exit 1;;
esac
''', 0o755)
env = re.search(r'Example `/etc/cncli/leaderlog.env`.*?```text\n(.*?)```', doc, re.S).group(1)
env = env.replace('REPLACE_WITH_YOUR_POOL_ID','01'*28).replace('jsonPoolTool=/etc/cncli/pooltool.json', 'jsonPoolTool=""').replace('binCnCli=/usr/local/bin/cncli','binCnCli=/usr/local/bin/cncli-fixture')
put('/etc/cncli/leaderlog.env', env+'\nTEST=1\n', 0o640)
run('chown', 'root:cncli-leaderlog', '/etc/cncli/leaderlog.env')
put('/usr/local/bin/cncli-sandbox-probe', '''#!/usr/bin/python3
import os, pathlib, sqlite3, sys
who=sys.argv[1]
def denied(path):
    try:
        with open(path,'rb') as f: f.read(1)
    except PermissionError: return
    raise AssertionError('read unexpectedly permitted: '+path)
if who in ('sync','sendtip'): denied('/etc/cncli/vrf.skey')
if who == 'sync': denied('/etc/cncli/pooltool.json')
try:
    pathlib.Path('/usr/local/bin/cncli-forbidden-write').write_text('forbidden')
except OSError: pass
else: raise AssertionError('executable directory writable')
if who == 'leaderlog':
    assert pathlib.Path('/etc/cncli/vrf.skey').read_bytes()
    con=sqlite3.connect('/var/lib/cncli/cncli.db')
    con.execute('create table if not exists fixture_assignments(slot integer)')
    con.execute('insert into fixture_assignments values(42)'); con.commit(); con.close()
    pathlib.Path('/var/lib/cncli-leaderlog/permission-output').write_text('allowed')
print('SANDBOX PROBE PASS',who,'uid',os.getuid(),flush=True)
''', 0o755)
for name in ('sync','sendtip','leaderlog'):
    directory='/etc/systemd/system/cncli-'+name+'.service.d'
    P(directory).mkdir()
    put(directory+'/probe.conf', '[Service]\nExecStartPre=/usr/local/bin/cncli-sandbox-probe '+name+'\n')
run('systemd-analyze','verify',*[ '/etc/systemd/system/'+u for u in ('cncli-sync.service','cncli-sendtip.service','cncli-leaderlog.service','cncli-leaderlog.timer')])
run('systemctl','daemon-reload')
listener=socket.socket(); listener.bind(('127.0.0.1',3000)); listener.listen(32); listener.settimeout(1)
unix=socket.socket(socket.AF_UNIX); unix.bind('/run/cardano-node/node.socket'); unix.listen()
os.chown('/run/cardano-node/node.socket', 0, __import__('grp').getgrnam('cncli-fixture-socket').gr_gid); os.chmod('/run/cardano-node/node.socket',0o660)
connections=[]; stopping=threading.Event()
def accept():
    while not stopping.is_set():
        try:
            conn, addr=listener.accept(); connections.append(conn)
            print('LOOPBACK ACCEPT',len(connections),addr,flush=True)
        except socket.timeout: pass
thread=threading.Thread(target=accept,daemon=True); thread.start()
try:
    run('systemctl','start','cncli-sync.service','cncli-sendtip.service')
    time.sleep(3)
    pids={name:run('systemctl','show','cncli-'+name+'.service','--property=MainPID','--value').stdout.strip() for name in ('sync','sendtip')}
    assert all(int(pid)>0 for pid in pids.values())
    for pid in pids.values():
        assert os.readlink('/proc/'+pid+'/exe') == '/usr/local/bin/cncli'
    time.sleep(14) # handshake deadline + retry interval: must see at least two connections per task
    assert len(connections)>=4, ('missing real connection retry',len(connections))
    for name,pid in pids.items():
        assert run('systemctl','show','cncli-'+name+'.service','--property=MainPID','--value').stdout.strip()==pid
        assert run('systemctl','is-active','cncli-'+name+'.service').stdout.strip()=='active'
    assert P('/var/lib/cncli/cncli.db').exists()
    run('systemctl','start','cncli-leaderlog.service')
    assert P('/var/lib/cncli-leaderlog/slots.csv').read_text().strip()=='fixture,42,1'
    print('ONESHOT SYNTHETIC EXTERNAL HELPER SUCCESS; not candidate leaderlog consensus evidence',flush=True)
    run('systemctl','start','cncli-leaderlog.timer')
    assert run('systemctl','is-active','cncli-leaderlog.timer').stdout.strip()=='active'
    for unit in ('sync.service','sendtip.service','leaderlog.service','leaderlog.timer'):
        result=run('systemctl','show','cncli-'+unit,'--property=User,NoNewPrivileges,ProtectSystem,ProtectHome,ActiveState,SubState,Result,MainPID,ExecMainStatus,NextElapseUSecRealtime')
        if unit.endswith('.service'):
            for property in ('NoNewPrivileges=yes','ProtectSystem=strict','ProtectHome=yes'): assert property in result.stdout
    # Actual candidate is expected to fail on deliberately empty genesis: no invented application success.
    r=run('runuser','-u','cncli-leaderlog','--','/usr/local/bin/cncli','status','--db','/var/lib/cncli/cncli.db','--byron-genesis','/etc/cncli/mainnet-byron-genesis.json','--shelley-genesis','/etc/cncli/mainnet-shelley-genesis.json',check=False)
    assert r.returncode==1 and json.loads(r.stdout)['status']=='error'
    print('ACTUAL CANDIDATE EMPTY-FIXTURE STATUS: expected exit 1/error JSON',flush=True)
    run('journalctl','-u','cncli-sync','-u','cncli-sendtip','-u','cncli-leaderlog','--no-pager','-n','160')
    print('PASS actual systemd mount sandbox probes, candidate executable lifecycle and TCP retry, oneshot synthetic CSV, timer properties',flush=True)
finally:
    run('systemctl','stop','cncli-leaderlog.timer','cncli-leaderlog.service','cncli-sync.service','cncli-sendtip.service',check=False)
    stopping.set(); listener.close(); unix.close()
    for conn in connections: conn.close()
