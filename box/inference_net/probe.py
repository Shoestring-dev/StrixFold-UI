#!/usr/bin/python3
"""Deliberate harmless outbound probes. Run through halo-inference-run on each box."""
import socket,json,os
out=[]
for name,af,kind,addr in [
 ('internet HTTPS',socket.AF_INET,socket.SOCK_STREAM,('1.1.1.1',443)),
 ('internet HTTP',socket.AF_INET,socket.SOCK_STREAM,('1.1.1.1',80)),
 ('DNS UDP',socket.AF_INET,socket.SOCK_DGRAM,('1.1.1.1',53)),
 ('LAN gateway',socket.AF_INET,socket.SOCK_STREAM,('192.168.1.1',80)),
 ('loopback proxy',socket.AF_INET,socket.SOCK_STREAM,('127.0.0.1',19876)),
 ('IPv6 loopback',socket.AF_INET6,socket.SOCK_STREAM,('::1',19876)),
 ('IPv6 internet',socket.AF_INET6,socket.SOCK_STREAM,('2606:4700:4700::1111',443)),
 ('Unix socket escape',socket.AF_UNIX,socket.SOCK_STREAM,'/run/user/1001/bus')]:
 s=None
 try:
  s=socket.socket(af,kind);s.settimeout(2);s.connect(addr)
  if kind==socket.SOCK_DGRAM:s.send(b'\x00'*12);s.recv(32)
  out.append({'test':name,'blocked':False})
 except OSError as e:out.append({'test':name,'blocked':True,'errno':e.errno,'reason':str(e)})
 finally:
  if s:s.close()
print(json.dumps({'cgroup':open('/proc/self/cgroup').read().strip(),'results':out},indent=2))
raise SystemExit(0 if all(r['blocked'] for r in out) else 1)
