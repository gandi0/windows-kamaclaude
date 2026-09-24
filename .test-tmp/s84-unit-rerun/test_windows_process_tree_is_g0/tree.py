import json, os, socket, subprocess, sys
if sys.argv[1] == 'parent':
    subprocess.Popen([sys.executable, __file__, 'child', sys.argv[2]])
s=socket.create_connection(('127.0.0.1',int(sys.argv[2])))
s.sendall((json.dumps({'pid':os.getpid()})+'\n').encode())
s.recv(1)
