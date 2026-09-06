# Start web control panel
import subprocess, sys, os

# Install flask if needed
try:
    import flask
except ImportError:
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'flask', '-q'])

# Run web server
os.chdir(os.path.dirname(os.path.abspath(__file__)))
print('='*50)
print('抖音自动发送 · 网页控制面板')
print('='*50)
print()
print('启动中...')
print('访问地址: http://127.0.0.1:5000')
print()
print('按 Ctrl+C 停止服务')
print()

from web.app import app
app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)
