#!/usr/bin/python3 -I
"""Enter network-restricted cgroup before exec, preserving PID and inference environment."""
import ctypes,os,subprocess,sys
if len(sys.argv)<2:raise SystemExit('Expected inference command')
subprocess.run(['/usr/bin/sudo','-n','/usr/local/libexec/halo-inference-enter',str(os.getpid())],check=True)
# Prevent sudo/setuid executables from escaping the cgroup after exec.
if ctypes.CDLL(None,use_errno=True).prctl(38,1,0,0,0)!=0:raise OSError(ctypes.get_errno(),'no_new_privs')
# Prevent local Unix-socket proxies (including user service managers) bypassing IP filtering.
class Arg(ctypes.Structure):
 _fields_=[('arg',ctypes.c_uint),('op',ctypes.c_uint),('a',ctypes.c_uint64),('b',ctypes.c_uint64)]
sec=ctypes.CDLL('libseccomp.so.2')
sec.seccomp_init.argtypes=[ctypes.c_uint32];sec.seccomp_init.restype=ctypes.c_void_p
sec.seccomp_syscall_resolve_name.argtypes=[ctypes.c_char_p];sec.seccomp_syscall_resolve_name.restype=ctypes.c_int
sec.seccomp_rule_add_array.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_int,ctypes.c_uint,ctypes.POINTER(Arg)]
sec.seccomp_load.argtypes=[ctypes.c_void_p];sec.seccomp_release.argtypes=[ctypes.c_void_p]
ctx=sec.seccomp_init(0x7fff0000)
if not ctx:raise RuntimeError('Cannot initialize network syscall policy')
rule=Arg(0,4,1,0)  # socket(domain == AF_UNIX), SCMP_CMP_EQ
if sec.seccomp_rule_add_array(ctx,0x00050001,sec.seccomp_syscall_resolve_name(b'socket'),1,ctypes.byref(rule))!=0 or sec.seccomp_load(ctx)!=0:
 raise RuntimeError('Cannot enforce network syscall policy')
sec.seccomp_release(ctx)
os.execvpe(sys.argv[1],sys.argv[1:],os.environ)
