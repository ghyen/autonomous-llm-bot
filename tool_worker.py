"""One-shot worker entrypoint; it intentionally has no bot or config imports."""

import json
import sys

import workspace_io


def _error(error):
    return {"status": "error", "error": error}


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
            return workspace_io.read_file(workspace, path)

        if operation == "write_file":
            path = request.get("path")
            if not isinstance(path, str):
                return _error("invalid_path")
            return workspace_io.write_file(
                workspace,
                path,
                request.get("content", ""),
                request.get("expected_revision"),
            )

        if operation in ("bash_exec", "web_search"):
            return _error("operation_not_ready")
        return _error("unsupported_operation")
    except (TypeError, ValueError, OSError):
        return _error("worker_operation_failed")


def main():
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
