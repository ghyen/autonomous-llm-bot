import asyncio
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from tool_sandbox import (
    _start_fixed_proxy,
    build_profile,
    resolve_network_addresses,
    run_worker,
)


class ProfileTest(unittest.TestCase):
    def test_profile_denies_by_default_and_contains_only_parameterized_workspace(self):
        profile = build_profile(Path("/tmp/run root"), (Path("/usr/bin/python3"),), ())
        self.assertIn("(deny default", profile)
        self.assertIn('(subpath (param "WORKSPACE"))', profile)
        self.assertNotIn("(allow network-outbound)\n", profile)
        self.assertNotIn("/Users/edwin", profile)

    def test_profile_escapes_parameter_values(self):
        profile = build_profile(Path('/tmp/run "root"'), (Path("/usr/bin/python3"),), ())
        self.assertIn('(subpath (param "WORKSPACE"))', profile)
        self.assertNotIn('/tmp/run "root"', profile)

    def test_network_addresses_are_deduplicated_and_resolved(self):
        addresses = resolve_network_addresses((("https", "localhost", 443),))
        self.assertIn(("127.0.0.1", 443), addresses)
        self.assertEqual(len(addresses), len(set(addresses)))


@unittest.skipUnless(sys.platform == "darwin", "macOS Seatbelt integration")
class SandboxIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_proxy_tunnels_only_the_approved_authority(self):
        async def echo(reader, writer):
            writer.write(await reader.read(2))
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]
        proxy = await _start_fixed_proxy(
            (("127.0.0.1", target_port),), "html.duckduckgo.com", target_port
        )
        proxy_port = proxy.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            writer.write(
                "CONNECT html.duckduckgo.com:{0} HTTP/1.1\r\n\r\n".format(
                    target_port
                ).encode()
            )
            await writer.drain()
            self.assertIn(b"200", await reader.readline())
            await reader.readline()
            writer.write(b"ok")
            await writer.drain()
            self.assertEqual(await reader.read(2), b"ok")
            writer.close()
            await writer.wait_closed()
        finally:
            proxy.close()
            await proxy.wait_closed()
            target.close()
            await target.wait_closed()

    async def test_sandboxed_worker_can_read_workspace_but_not_outside(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            Path(root, "inside.txt").write_text("inside", encoding="utf-8")
            canary = Path(outside, "secret.txt")
            canary.write_text("PRIVATE-KEY-CANARY", encoding="utf-8")
            inside = await run_worker(
                root,
                {"operation": "read_file", "workspace": root, "path": "inside.txt"},
                timeout=5,
            )
            denied = await run_worker(
                root,
                {
                    "operation": "bash_exec",
                    "workspace": root,
                    "command": "cat {0}".format(shlex.quote(str(canary))),
                },
                timeout=5,
            )

        self.assertEqual(inside["status"], "success")
        self.assertEqual(inside["content"], "inside")
        self.assertTrue(
            denied.get("status") != "success" or denied.get("exit_code", 0) != 0,
            denied,
        )
        self.assertNotIn("PRIVATE-KEY-CANARY", json.dumps(denied))

    async def test_network_is_denied_by_default_and_allowed_for_one_endpoint(self):
        async def serve(reader, writer):
            writer.write(b"ok")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        command = (
            "python3 -c "
            + shlex.quote(
                "import socket; s=socket.create_connection(('127.0.0.1', %d), 1); "
                "print(s.recv(2).decode())" % port
            )
        )
        try:
            denied = await run_worker(
                tempfile.gettempdir(),
                {
                    "operation": "bash_exec",
                    "workspace": tempfile.gettempdir(),
                    "command": command,
                },
                timeout=5,
            )
            with tempfile.TemporaryDirectory() as root:
                allowed = await run_worker(
                    root,
                    {
                        "operation": "bash_exec",
                        "workspace": root,
                        "command": command,
                        "network_allowlist": [("http", "127.0.0.1", port)],
                    },
                    timeout=5,
                )
        finally:
            server.close()
            await server.wait_closed()

        self.assertNotEqual(denied.get("exit_code"), 0)
        self.assertEqual(allowed["status"], "success", allowed)
        self.assertIn("ok", allowed["stdout"])

    async def test_output_disk_process_thread_and_cpu_limits_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            output = await run_worker(
                root,
                {
                    "operation": "bash_exec",
                    "workspace": root,
                    "command": "python3 -c 'print(\"x\" * 200000)'",
                    "limits": {"output_bytes": 1024},
                },
                timeout=5,
            )
            disk = await run_worker(
                root,
                {
                    "operation": "bash_exec",
                    "workspace": root,
                    "command": "dd if=/dev/zero of=blob bs=4096 count=4",
                    "limits": {"disk_bytes": 1024},
                },
                timeout=5,
            )
            processes = await run_worker(
                root,
                {
                    "operation": "bash_exec",
                    "workspace": root,
                    "command": "for i in $(seq 1 8); do sleep 2 & done; wait",
                    "limits": {"process_limit": 2},
                },
                timeout=5,
            )
            threads = await run_worker(
                root,
                {
                    "operation": "bash_exec",
                    "workspace": root,
                    "command": "python3 -c 'import threading,time; [threading.Thread(target=time.sleep,args=(2,)).start() for _ in range(8)]; time.sleep(2)'",
                    "limits": {"thread_limit": 4},
                },
                timeout=5,
            )
            cpu = await run_worker(
                root,
                {
                    "operation": "bash_exec",
                    "workspace": root,
                    "command": "while :; do :; done",
                    "limits": {"cpu_seconds": 1},
                },
                timeout=5,
            )
            memory = await run_worker(
                root,
                {
                    "operation": "bash_exec",
                    "workspace": root,
                    "command": "python3 -c 'x=bytearray(80*1024*1024); import time; time.sleep(2)'",
                    "limits": {"memory_bytes": 33554432},
                },
                timeout=5,
            )

        self.assertEqual(output["status"], "resource_limit", output)
        self.assertEqual(disk["status"], "resource_limit", disk)
        self.assertEqual(processes["status"], "resource_limit", processes)
        self.assertEqual(threads["status"], "resource_limit", threads)
        self.assertEqual(cpu["status"], "resource_limit", cpu)
        self.assertEqual(memory["status"], "resource_limit", memory)

    async def test_worker_timeout_reaps_the_child_group(self):
        with tempfile.TemporaryDirectory() as root:
            result = await run_worker(
                root,
                {
                    "operation": "bash_exec",
                    "workspace": root,
                    "command": "sleep 10 & echo $! > child.pid; wait",
                    "timeout": 0.2,
                },
                timeout=5,
            )
            child_pid = int(Path(root, "child.pid").read_text())
        await asyncio.sleep(0.2)
        with self.assertRaises(OSError):
            os.kill(child_pid, 0)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "worker_timeout")
