"""Run the regression suite without external network or real Codex inference.

Loopback HTTP stubs and FFmpeg are allowed. Individual tests can still replace
Popen/socket with their own mocks. No production credentials are changed.
"""
from pathlib import Path
import argparse
import ipaddress
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern',default='test_*.py')
    args=parser.parse_args()
    # Test modules use import-time FFmpeg discovery; prefer the already configured
    # executables, without requiring a global PATH edit on Windows.
    from core.config import load_config
    cfg=load_config()
    for variable,key in [('FCF_TEST_FFMPEG','ffmpeg'),('FCF_TEST_FFPROBE','ffprobe')]:
        if Path(cfg[key]).is_file():
            import os
            os.environ.setdefault(variable,cfg[key])
    connect,connect_ex,popen=socket.socket.connect,socket.socket.connect_ex,subprocess.Popen
    getaddrinfo=socket.getaddrinfo

    def allowed(address):
        if not isinstance(address,tuple):return False
        host=address[0]
        if host=='localhost':return True
        try:return ipaddress.ip_address(host).is_loopback
        except ValueError:return False

    def guarded_connect(sock,address):
        if not allowed(address):raise OSError('Offline regression blocked external network')
        return connect(sock,address)

    def guarded_connect_ex(sock,address):
        if not allowed(address):raise OSError('Offline regression blocked external network')
        return connect_ex(sock,address)

    def guarded_getaddrinfo(host,port,*a,**kw):
        if not allowed((host,port)):raise OSError('Offline regression blocked external DNS')
        return getaddrinfo(host,port,*a,**kw)

    def guarded_popen(command,*a,**kw):
        if isinstance(command,(tuple,list)) and command:
            executable=Path(str(command[0])).stem.lower()
            actual_cli=(executable=='codex' or (executable=='node' and len(command)>1 and 'codex' in str(command[1]).lower()))
            if actual_cli and 'exec' in command:
                raise OSError('Offline regression blocked real Codex inference')
        return popen(command,*a,**kw)

    with patch.object(socket.socket,'connect',guarded_connect),patch.object(socket.socket,'connect_ex',guarded_connect_ex), \
         patch('socket.getaddrinfo',guarded_getaddrinfo),patch('subprocess.Popen',guarded_popen):
        suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=args.pattern)
        result=unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
