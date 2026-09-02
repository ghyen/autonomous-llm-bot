"""One-shot worker entrypoint; it intentionally has no bot or config imports."""

import ctypes
import importlib.util
import json
import math
import os
import resource
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


_WORKSPACE_IO_PATH = Path(__file__).with_name("workspace_io.py")
_WORKSPACE_IO_SPEC = importlib.util.spec_from_file_location(
    "tool_worker_workspace_io", _WORKSPACE_IO_PATH
)
if _WORKSPACE_IO_SPEC is None or _WORKSPACE_IO_SPEC.loader is None:
    raise RuntimeError("workspace I/O module is unavailable")
workspace_io = importlib.util.module_from_spec(_WORKSPACE_IO_SPEC)
_WORKSPACE_IO_SPEC.loader.exec_module(workspace_io)


_VIRTUALENV_BIN = Path(sys.prefix) / "bin"
FIXED_PATH = os.pathsep.join(
    str(path)
    for path in (_VIRTUALENV_BIN, Path("/usr/bin"), Path("/bin"), Path("/usr/sbin"), Path("/sbin"))
    if path.is_dir()
)
DEFAULT_TIMEOUT = 60.0
DEFAULT_FILE_BYTES = 10485760
MONITOR_INTERVAL = 0.05
_active_process = None
_active_process_lock = threading.Lock()


class ResourceLimitError(Exception):
    """A required worker ceiling could not be installed or was exceeded."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _error(error):
    return {"status": "error", "error": error}


def _resource_limit(reason):
    return {"status": "resource_limit", "error": reason}


def _set_resource_limit(name, raw_value):
    limit_name = getattr(resource, name, None)
    if limit_name is None:
        raise ResourceLimitError("unsupported_" + name.lower())
    try:
        value = int(math.ceil(float(raw_value)))
        if value <= 0:
            raise ValueError
        _soft, hard = resource.getrlimit(limit_name)
        if hard != resource.RLIM_INFINITY and value > hard:
            raise ValueError
        resource.setrlimit(limit_name, (value, value))
    except (OSError, ValueError, TypeError, OverflowError):
        raise ResourceLimitError("unable_to_set_" + name.lower())


def apply_limits(limits):
    """Install every required kernel ceiling before running an OS-facing tool."""
    if not isinstance(limits, dict):
        raise ResourceLimitError("invalid_limits")
    for name, key in (
        ("RLIMIT_CPU", "cpu_seconds"),
        ("RLIMIT_NOFILE", "open_files"),
        ("RLIMIT_FSIZE", "file_bytes"),
    ):
        if key not in limits:
            raise ResourceLimitError("missing_" + key)
        _set_resource_limit(name, limits[key])
    if "memory_bytes" not in limits or limits["memory_bytes"] <= 0:
        raise ResourceLimitError("missing_memory_bytes")
    # macOS exposes RLIMIT_AS but rejects lowering its infinite process-wide
    # address-space ceiling. The monitor below is the enforceable memory bound;
    # other platforms must still provide the kernel limit rather than silently
    # falling back.
    try:
        _set_resource_limit("RLIMIT_AS", limits["memory_bytes"])
    except ResourceLimitError:
        if sys.platform != "darwin":
            raise
    core = getattr(resource, "RLIMIT_CORE", None)
    if core is None:
        raise ResourceLimitError("unsupported_rlimit_core")
    try:
        resource.setrlimit(core, (0, 0))
    except (OSError, ValueError):
        raise ResourceLimitError("unable_to_set_rlimit_core")


def workspace_usage(root):
    """Count logical bytes without following symlinks out of the workspace."""
    total = 0
    pending = [Path(root)]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as error:
            raise ResourceLimitError("workspace_usage_unavailable") from error
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise ResourceLimitError("workspace_usage_unavailable") from error
            total += int(info.st_size)
            if entry.is_dir(follow_symlinks=False):
                pending.append(Path(entry.path))
    return total


class _ProcTaskInfo(ctypes.Structure):
    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("total_user", ctypes.c_uint64),
        ("total_system", ctypes.c_uint64),
        ("threads_user", ctypes.c_uint64),
        ("threads_system", ctypes.c_uint64),
        ("policy", ctypes.c_int32),
        ("faults", ctypes.c_int32),
        ("pageins", ctypes.c_int32),
        ("cow_faults", ctypes.c_int32),
        ("messages_sent", ctypes.c_int32),
        ("messages_received", ctypes.c_int32),
        ("syscalls_mach", ctypes.c_int32),
        ("syscalls_unix", ctypes.c_int32),
        ("csw", ctypes.c_int32),
        ("threadnum", ctypes.c_int32),
        ("numrunning", ctypes.c_int32),
        ("priority", ctypes.c_int32),
    ]


def _task_info(pid):
    try:
        libsystem = ctypes.CDLL(None)
        proc_pidinfo = libsystem.proc_pidinfo
        proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.POINTER(_ProcTaskInfo),
            ctypes.c_int,
        ]
        proc_pidinfo.restype = ctypes.c_int
        info = _ProcTaskInfo()
        result = proc_pidinfo(
            int(pid), 4, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
        if result != ctypes.sizeof(info):
            raise OSError("proc_pidinfo failed")
        return info
    except (AttributeError, OSError, TypeError, ValueError):
        raise ResourceLimitError("process_info_unavailable")


def _child_pids(pid):
    try:
        libsystem = ctypes.CDLL(None)
        list_children = libsystem.proc_listchildpids
        list_children.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        list_children.restype = ctypes.c_int
        size = 4096
        while size <= 1024 * 1024:
            buffer = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int)))()
            result = list_children(int(pid), buffer, size)
            if result < 0:
                raise OSError("proc_listchildpids failed")
            if result < size:
                count = result // ctypes.sizeof(ctypes.c_int)
                return [int(buffer[index]) for index in range(count) if buffer[index] > 0]
            size *= 2
        raise OSError("child process list is too large")
    except (AttributeError, OSError, TypeError, ValueError) as error:
        raise ResourceLimitError("process_count_unavailable") from error


def _process_tree(root_pid):
    descendants = {int(root_pid)}
    pending = [int(root_pid)]
    while pending:
        for child in _child_pids(pending.pop()):
            if child not in descendants:
                descendants.add(child)
                pending.append(child)
    return descendants


def process_tree_counts(root_pid):
    """Return (processes, threads) for the worker's child process tree."""
    descendants = _process_tree(root_pid)
    threads = sum(_task_info(pid).threadnum for pid in descendants)
    return len(descendants), threads


def process_tree_memory(root_pid):
    return sum(_task_info(pid).resident_size for pid in _process_tree(root_pid))


def process_memory_bytes(pid):
    return _task_info(pid).resident_size


def _kill_group(process, include_exited=False):
    if process is None or (process.poll() is not None and not include_exited):
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


class _Output:
    def __init__(self, limit):
        self.limit = int(limit)
        self.total = 0
        self.buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self.overflow = False
        self.error = None
        self.lock = threading.Lock()

    def append(self, name, chunk):
        with self.lock:
            remaining = self.limit - self.total
            if remaining <= 0:
                self.overflow = True
                return False
            accepted = chunk[:remaining]
            self.buffers[name].extend(accepted)
            self.total += len(accepted)
            if len(accepted) != len(chunk):
                self.overflow = True
                return False
            return True


def _read_output(stream, name, output):
    try:
        while not output.overflow:
            chunk = stream.read(8192)
            if not chunk:
                break
            if not output.append(name, chunk):
                break
    except OSError as error:
        output.error = type(error).__name__


def _child_environment(root, display_root=None):
    display_root = str(display_root or root)
    return {
        "PATH": FIXED_PATH,
        "LANG": "C",
        "LC_ALL": "C",
        "HOME": display_root,
        "TMPDIR": display_root,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _active_process_for_signal(signum, _frame):
    with _active_process_lock:
        process = _active_process
    _kill_group(process)
    if signum == signal.SIGTERM:
        raise SystemExit(143)


def _bash(request):
    root = Path(request["workspace"])
    display_root = request.get("display_workspace")
    command = request.get("command")
    limits = request["limits"]
    if not isinstance(command, str):
        return _error("invalid_command")
    try:
        timeout = float(request.get("timeout", DEFAULT_TIMEOUT))
        if timeout <= 0 or not math.isfinite(timeout):
            return _error("invalid_timeout")
    except (TypeError, ValueError):
        return _error("invalid_timeout")

    try:
        apply_limits(limits)
    except ResourceLimitError as error:
        return _resource_limit(error.reason)

    output = _Output(limits["output_bytes"])
    try:
        process = subprocess.Popen(
            ["/bin/bash", "-c", command],
            cwd=str(root),
            env=_child_environment(root, display_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return _error("worker_process_start_failed")

    global _active_process
    with _active_process_lock:
        _active_process = process
    readers = [
        threading.Thread(target=_read_output, args=(process.stdout, "stdout", output), daemon=True),
        threading.Thread(target=_read_output, args=(process.stderr, "stderr", output), daemon=True),
    ]
    for reader in readers:
        reader.start()

    reason = None
    started = time.monotonic()
    try:
        while process.poll() is None:
            if output.overflow:
                reason = "worker_output_limit"
                break
            if output.error:
                reason = "worker_output_unavailable"
                break
            if time.monotonic() - started > timeout:
                reason = "worker_timeout"
                break
            try:
                if workspace_usage(root) > limits["disk_bytes"]:
                    reason = "workspace_disk_limit"
                    break
                processes, threads = process_tree_counts(process.pid)
                if (
                    process_tree_memory(process.pid) + process_memory_bytes(os.getpid())
                    > limits["memory_bytes"]
                ):
                    reason = "memory_limit"
                    break
            except ResourceLimitError as error:
                reason = error.reason
                break
            if processes > limits["process_limit"]:
                reason = "process_limit"
                break
            if threads + threading.active_count() > limits["thread_limit"]:
                reason = "thread_limit"
                break
            time.sleep(MONITOR_INTERVAL)
        if reason is not None:
            _kill_group(process)
        else:
            process.wait()
            _kill_group(process, include_exited=True)
            try:
                if workspace_usage(root) > limits["disk_bytes"]:
                    reason = "workspace_disk_limit"
            except ResourceLimitError as error:
                reason = error.reason
            if reason is None and process.returncode in (-signal.SIGXCPU, -signal.SIGXFSZ):
                reason = "kernel_resource_limit"
    finally:
        for reader in readers:
            reader.join(timeout=1)
        if reason is None:
            if output.overflow:
                reason = "worker_output_limit"
            elif output.error:
                reason = "worker_output_unavailable"
        with _active_process_lock:
            _active_process = None

    if reason:
        if reason == "worker_timeout":
            return _error(reason)
        return _resource_limit(reason)
    return {
        "status": "success",
        "stdout": bytes(output.buffers["stdout"]).decode("utf-8", errors="replace"),
        "stderr": bytes(output.buffers["stderr"]).decode("utf-8", errors="replace"),
        "exit_code": process.returncode,
    }


def _web_search(request):
    query = request.get("query")
    if not isinstance(query, str) or not query.strip():
        return _error("invalid_query")
    entries = request.get("network_allowlist") or ()
    if not any(
        isinstance(entry, (list, tuple))
        and len(entry) == 3
        and str(entry[0]).lower() == "https"
        and str(entry[1]).lower() == "html.duckduckgo.com"
        and int(entry[2]) == 443
        for entry in entries
    ):
        return _error("network_not_allowed")
    proxy_url = request.get("proxy_url")
    if not isinstance(proxy_url, str) or not proxy_url.startswith("http://127.0.0.1:"):
        return _error("network_proxy_unavailable")
    try:
        timeout = float(request.get("timeout", DEFAULT_TIMEOUT))
        if timeout <= 0 or not math.isfinite(timeout):
            return _error("invalid_timeout")
    except (TypeError, ValueError):
        return _error("invalid_timeout")
    limits = request.get("limits")
    try:
        apply_limits(limits)
        from duckduckgo_search import DDGS
    except ResourceLimitError as error:
        return _resource_limit(error.reason)
    except Exception:
        return _error("web_search_unavailable")

    result_box = {}
    done = threading.Event()

    def search():
        try:
            with DDGS(proxy=proxy_url, timeout=max(1, int(timeout))) as ddgs:
                html_search = getattr(ddgs, "_text_html", None)
                if not callable(html_search):
                    result_box["error"] = "html_search_backend_unavailable"
                else:
                    result_box["results"] = list(html_search(query, max_results=5))
        except Exception:
            result_box["error"] = "web_search_failed"
        finally:
            done.set()

    threading.Thread(target=search, daemon=True).start()
    started = time.monotonic()
    while not done.wait(MONITOR_INTERVAL):
        if time.monotonic() - started > timeout:
            return _error("worker_timeout")
        try:
            if process_memory_bytes(os.getpid()) > limits["memory_bytes"]:
                return _resource_limit("memory_limit")
        except ResourceLimitError as error:
            return _resource_limit(error.reason)
        if threading.active_count() > limits["thread_limit"]:
            return _resource_limit("thread_limit")
    if "error" in result_box:
        return _error(result_box["error"])
    results = result_box.get("results", [])
    return {"status": "success", "results": results}


def handle_request(request):
    if not isinstance(request, dict):
        return _error("invalid_request")
    operation = request.get("operation")
    workspace = request.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        return _error("invalid_workspace")

    try:
        if operation == "read_file":
            path = request.get("path")
            if not isinstance(path, str):
                return _error("invalid_path")
            limits = request.get("limits") or {}
            max_bytes = limits.get("file_bytes", DEFAULT_FILE_BYTES)
            if not isinstance(max_bytes, (int, float)) or isinstance(max_bytes, bool) or max_bytes <= 0:
                return _resource_limit("invalid_file_bytes")
            return workspace_io.read_file(workspace, path, max_bytes=max_bytes)

        if operation == "write_file":
            path = request.get("path")
            if not isinstance(path, str):
                return _error("invalid_path")
            limits = request.get("limits") or {}
            max_bytes = limits.get("file_bytes", DEFAULT_FILE_BYTES)
            try:
                content_bytes = str(request.get("content", "")).encode("utf-8")
            except (UnicodeEncodeError, TypeError):
                return _error("invalid_content")
            if not isinstance(max_bytes, (int, float)) or isinstance(max_bytes, bool) or max_bytes <= 0:
                return _resource_limit("invalid_file_bytes")
            if len(content_bytes) > max_bytes:
                return _resource_limit("file_bytes_limit")
            return workspace_io.write_file(
                workspace,
                path,
                request.get("content", ""),
                request.get("expected_revision"),
            )

        if operation == "bash_exec":
            return _bash(request)

        if operation == "web_search":
            return _web_search(request)
        return _error("unsupported_operation")
    except (TypeError, ValueError, OSError, KeyError):
        return _error("worker_operation_failed")


def main():
    signal.signal(signal.SIGTERM, _active_process_for_signal)
    line = sys.stdin.buffer.readline()
    if not line.strip():
        result = _error("empty_request")
    else:
        extra = sys.stdin.buffer.read()
        if extra.strip():
            result = _error("multiple_requests")
        else:
            try:
                result = handle_request(json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, ValueError, TypeError):
                result = _error("invalid_json")
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
