"""macOS Seatbelt supervisor for disposable tool workers."""

import asyncio
import ipaddress
import json
import math
import os
import socket
import sys
import sysconfig
from pathlib import Path

import workspace_io


SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
WORKER_PATH = Path(__file__).resolve().with_name("tool_worker.py")
DEFAULT_LIMITS = {
    "cpu_seconds": 30.0,
    "memory_bytes": 268435456,
    "process_limit": 32,
    "thread_limit": 64,
    "open_files": 64,
    "file_bytes": 10485760,
    "output_bytes": 65536,
    "disk_bytes": 52428800,
}


def _sbpl_param(name):
    return '(param "{0}")'.format(name)


def _remote_address(address, port):
    try:
        if ipaddress.ip_address(address).is_loopback:
            return "localhost:{0}".format(port)
    except ValueError:
        raise RuntimeError("network address is not numeric")
    if ":" in address:
        raise RuntimeError("macOS Seatbelt cannot filter IPv6 network destinations")
    raise RuntimeError("macOS Seatbelt cannot filter non-local network destinations")


def build_profile(workspace, runtime_paths, network_addresses):
    """Build a deny-by-default profile without interpolating model input."""
    del workspace
    del runtime_paths  # Values are passed with -D; profile text stays constant.
    lines = [
        "(version 1)",
        "(deny default (with no-callout))",
        # Reading the root vnode lets dyld/Python traverse approved paths; it
        # does not grant file contents outside the rules below.
        "(allow file-read* (literal \"/\"))",
        "(allow file-read-metadata)",
        "(allow file-read* (subpath {0}))".format(_sbpl_param("WORKSPACE")),
        "(allow file-write* (subpath {0}))".format(_sbpl_param("WORKSPACE")),
        "(allow file-read* (literal {0}) (literal {1}))".format(
            _sbpl_param("WORKER"), _sbpl_param("WORKSPACE_IO")
        ),
        "(allow file-read-metadata (subpath {0}))".format(_sbpl_param("WORKER_DIR")),
        "(allow file-read* (subpath {0}))".format(_sbpl_param("PYTHON_PREFIX")),
        "(allow file-read* (subpath {0}))".format(_sbpl_param("PYTHON_LIBDIR")),
        "(allow file-read* (subpath {0}))".format(_sbpl_param("PYTHON_STDLIB")),
        "(allow file-read* (subpath {0}))".format(_sbpl_param("PYTHON_SITE")),
        "(allow file-read* (subpath \"/System\") (subpath \"/usr\") (subpath \"/bin\") (subpath \"/sbin\") (subpath \"/opt/homebrew\"))",
        "(allow file-read* file-write-data (literal \"/dev/null\") (literal \"/dev/urandom\") (literal \"/dev/random\") (literal \"/dev/zero\"))",
        "(allow file-read* (literal \"/private/etc/hosts\") (literal \"/private/etc/resolv.conf\") (literal \"/private/etc/protocols\") (literal \"/private/etc/services\") (literal \"/private/etc/ssl/cert.pem\"))",
        "(allow file-read* file-write-data (literal \"/private/var/run/mDNSResponder\"))",
        "(allow file-map-executable (literal {0}) (subpath {1}) (subpath \"/System\") (subpath \"/usr\") (subpath \"/bin\") (subpath \"/sbin\") (subpath \"/opt/homebrew\"))".format(
            _sbpl_param("PYTHON_EXECUTABLE"), _sbpl_param("PYTHON_PREFIX")
        ),
        "(allow process-fork)",
        "(allow process-exec*)",
        "(allow signal (target same-sandbox))",
        "(allow process-info* (target self))",
        "(allow process-info-listpids)",
        "(allow process-info-pidinfo)",
        "(allow sysctl-read)",
        "(allow ipc-posix-shm)",
        "(allow mach-lookup (global-name \"com.apple.mDNSResponder\") (global-name \"com.apple.system.logger\"))",
    ]
    for address, port in network_addresses:
        lines.append(
            '(allow network-outbound (remote tcp "{0}"))'.format(
                _remote_address(address, port)
            )
        )
    return "\n".join(lines) + "\n"


def resolve_network_addresses(entries):
    """Resolve operator-approved origins before compiling the profile."""
    addresses = set()
    for _scheme, host, port in entries:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            addresses.add((str(literal), int(port)))
            continue
        try:
            results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as error:
            raise RuntimeError("network destination could not be resolved") from error
        for family, _socktype, _proto, _canonname, sockaddr in results:
            if family == socket.AF_INET:
                addresses.add((sockaddr[0], int(port)))
            elif family == socket.AF_INET6:
                addresses.add((sockaddr[0].split("%", 1)[0], int(port)))
        if not results:
            raise RuntimeError("network destination could not be resolved")
    return tuple(sorted(addresses))


def _runtime_parameters():
    stdlib = sysconfig.get_path("stdlib")
    site = sysconfig.get_path("purelib") or sysconfig.get_path("platlib")
    libdir = sysconfig.get_config_var("LIBDIR")
    values = {
        "PYTHON_EXECUTABLE": str(Path(sys.executable).resolve()),
        "PYTHON_PREFIX": str(Path(sys.prefix).resolve()),
        "PYTHON_LIBDIR": str(Path(libdir).resolve()) if libdir else "",
        "PYTHON_STDLIB": str(Path(stdlib).resolve()) if stdlib else "",
        "PYTHON_SITE": str(Path(site).resolve()) if site else "",
        "WORKER": str(WORKER_PATH),
        "WORKSPACE_IO": str(Path(workspace_io.__file__).resolve()),
        "WORKER_DIR": str(WORKER_PATH.parent),
    }
    if not all(values.values()):
        raise RuntimeError("python runtime paths are unavailable")
    return values


def _worker_limits(request):
    limits = dict(DEFAULT_LIMITS)
    supplied = request.get("limits")
    if supplied is not None:
        if not isinstance(supplied, dict):
            raise ValueError("invalid worker limits")
        limits.update(supplied)
    for name, value in limits.items():
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ValueError("invalid worker limit")
    return limits


def _worker_environment(workspace):
    root = str(Path(os.path.realpath(os.fspath(workspace))))
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C",
        "LC_ALL": "C",
        "HOME": root,
        "TMPDIR": root,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


async def _relay(reader, writer):
    while True:
        chunk = await reader.read(16384)
        if not chunk:
            return
        writer.write(chunk)
        await writer.drain()


def _connect_target(authority):
    if "@" in authority:
        return None, None
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0 or authority[closing + 1 : closing + 2] != ":":
            return None, None
        host = authority[1:closing]
        raw_port = authority[closing + 2 :]
    else:
        host, separator, raw_port = authority.rpartition(":")
        if not separator:
            return None, None
    try:
        port = int(raw_port)
    except ValueError:
        return None, None
    return host, port


async def _start_fixed_proxy(targets, target_host, target_port):
    async def handle(reader, writer):
        upstream = None
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=2)
            if len(request_line) > 4096:
                return
            fields = request_line.decode("ascii").split()
            if len(fields) != 3 or fields[0].upper() != "CONNECT":
                writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            host, port = _connect_target(fields[1])
            if host is None or host.casefold() != target_host.casefold() or port != target_port:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            header_bytes = 0
            while True:
                header = await asyncio.wait_for(reader.readline(), timeout=2)
                header_bytes += len(header)
                if header_bytes > 8192:
                    return
                if header in (b"\r\n", b"\n", b""):
                    break
            for address in targets:
                try:
                    upstream = await asyncio.wait_for(
                        asyncio.open_connection(address[0], address[1]), timeout=5
                    )
                    break
                except (OSError, asyncio.TimeoutError):
                    upstream = None
            if upstream is None:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            upstream_reader, upstream_writer = upstream
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            relay_tasks = {
                asyncio.create_task(_relay(reader, upstream_writer)),
                asyncio.create_task(_relay(upstream_reader, writer)),
            }
            await asyncio.wait(relay_tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in relay_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*relay_tasks, return_exceptions=True)
            upstream_writer.close()
            await upstream_writer.wait_closed()
        except (OSError, UnicodeDecodeError, asyncio.TimeoutError):
            return
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


def _web_target_entry(entries):
    for entry in entries:
        if (
            isinstance(entry, (list, tuple))
            and len(entry) == 3
            and str(entry[0]).lower() == "https"
            and str(entry[1]).lower() == "html.duckduckgo.com"
            and int(entry[2]) == 443
        ):
            return (str(entry[0]), str(entry[1]), int(entry[2]))
    return None


async def _read_bounded(stream, limit):
    chunks = []
    total = 0
    while True:
        chunk = await stream.read(min(8192, limit + 1))
        if not chunk:
            return b"".join(chunks), False
        total += len(chunk)
        if total > limit:
            return b"".join(chunks), True
        chunks.append(chunk)


async def _kill_process_group(proc):
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, 15)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=0.5)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        await proc.wait()


async def _collect_process(proc, timeout, token, response_limit):
    stdout_task = asyncio.create_task(_read_bounded(proc.stdout, response_limit))
    stderr_task = asyncio.create_task(_read_bounded(proc.stderr, 8192))
    wait_task = asyncio.create_task(proc.wait())
    cancel_task = asyncio.create_task(token.wait()) if token is not None else None
    tasks = {stdout_task, stderr_task, wait_task}
    if cancel_task is not None:
        tasks.add(cancel_task)
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while True:
            if stdout_task.done() and stdout_task.result()[1]:
                await _kill_process_group(proc)
                return None, None, "output_limit"
            if stderr_task.done() and stderr_task.result()[1]:
                await _kill_process_group(proc)
                return None, None, "worker_unavailable"
            if wait_task.done() and stdout_task.done() and stderr_task.done():
                stdout, _ = stdout_task.result()
                stderr, _ = stderr_task.result()
                return stdout, stderr, None
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await _kill_process_group(proc)
                return None, None, "timeout"
            done, _ = await asyncio.wait(
                tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                await _kill_process_group(proc)
                return None, None, "timeout"
            if cancel_task is not None and cancel_task in done:
                await _kill_process_group(proc)
                raise asyncio.CancelledError
    finally:
        if cancel_task is not None:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        for task in (stdout_task, stderr_task, wait_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, wait_task, return_exceptions=True)


async def run_worker(workspace, request, timeout, token=None):
    """Run exactly one request in a fresh Seatbelt process; never falls back."""
    if sys.platform != "darwin" or not SANDBOX_EXEC.is_file() or not os.access(
        str(SANDBOX_EXEC), os.X_OK
    ):
        return {"status": "error", "error": "sandbox_unavailable"}
    if not isinstance(request, dict):
        return {"status": "error", "error": "invalid_request"}
    proxy_server = None
    try:
        root = Path(os.path.realpath(os.fspath(workspace)))
        if not root.is_dir():
            return {"status": "error", "error": "invalid_workspace"}
        limits = _worker_limits(request)
        entries = tuple(request.get("network_allowlist") or ())
        target_entry = _web_target_entry(entries) if request.get("operation") == "web_search" else None
        if target_entry is not None:
            target_addresses = resolve_network_addresses((target_entry,))
            proxy_server = await _start_fixed_proxy(
                target_addresses, target_entry[1], target_entry[2]
            )
            addresses = (("127.0.0.1", proxy_server.sockets[0].getsockname()[1]),)
        else:
            addresses = resolve_network_addresses(entries)
        parameters = _runtime_parameters()
        parameters["WORKSPACE"] = str(root)
        profile = build_profile(root, tuple(Path(value) for value in parameters.values()), addresses)
        command = [str(SANDBOX_EXEC)]
        for key, value in parameters.items():
            command.extend(("-D", "{0}={1}".format(key, value)))
        command.extend(("-p", profile, sys.executable, "-B", str(WORKER_PATH)))
        payload = dict(request)
        payload["workspace"] = str(root)
        payload["limits"] = limits
        payload["network_allowlist"] = list(entries)
        if proxy_server is not None:
            payload["proxy_url"] = "http://127.0.0.1:{0}".format(
                proxy_server.sockets[0].getsockname()[1]
            )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=_worker_environment(root),
        )
        process.stdin.write(
            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
        )
        await process.stdin.drain()
        process.stdin.close()
        stdout, stderr, failure = await _collect_process(
            process,
            float(timeout),
            token,
            int(limits["output_bytes"]) + 8192,
        )
    except asyncio.CancelledError:
        if "process" in locals():
            await _kill_process_group(process)
        raise
    except (OSError, TypeError, ValueError, RuntimeError):
        return {"status": "error", "error": "sandbox_unavailable"}
    finally:
        if proxy_server is not None:
            proxy_server.close()
            await proxy_server.wait_closed()

    if failure == "timeout":
        return {"status": "error", "error": "worker_timeout"}
    if failure == "output_limit":
        return {"status": "resource_limit", "error": "worker_output_limit"}
    if failure:
        return {"status": "error", "error": failure}
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        return {"status": "error", "error": "worker_unavailable"}
    if not isinstance(result, dict) or not isinstance(result.get("status"), str):
        return {"status": "error", "error": "worker_unavailable"}
    return result
