import ast, os, sys
os.chdir(r'C:\MirageWork\mcp-server-v2')
files = [
    'server.py', 'llm.py', 'config.py', 'fallback.py',
    'tools/memory.py', 'tools/system.py', 'tools/device.py',
    'tools/build.py', 'tools/task.py', 'tools/loop.py',
    'tools/pipeline.py', 'tools/vision.py',
    'memory/compact.py', 'memory/store.py',
]
all_ok = True
for f in files:
    try:
        ast.parse(open(f, encoding='utf-8').read())
        sys.stdout.write(f'OK: {f}\n')
    except Exception as e:
        sys.stdout.write(f'NG: {f}: {e}\n')
        all_ok = False
sys.stdout.write(f'\n{"ALL OK" if all_ok else "SOME FAILED"}\n')
sys.stdout.flush()
