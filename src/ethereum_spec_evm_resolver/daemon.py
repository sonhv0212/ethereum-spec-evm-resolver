import json
import os
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socket import socket
from threading import Thread
from time import sleep
from typing import Any, List, Optional, Tuple, Union
from urllib.parse import urlunparse, quote, urlparse

from requests.exceptions import ConnectionError
from requests_unixsocket import Session

from .forks import (
    get_fork_resolution,
    get_fork_resolution_info,
)


runtime_dir = Path(tempfile.TemporaryDirectory().name)


class _EvmToolHandler(BaseHTTPRequestHandler):
    def log_request(self, *args):
        """Don't log requests"""
        pass

    def do_POST(self) -> None:
        content_length = int(self.headers["Content-Length"])
        content_bytes = self.rfile.read(content_length)
        content = json.loads(content_bytes)

        fork = content["state"]["fork"]

        self.server.spawn_subserver(fork)

        response = Session().post(
            self.server.get_subserver_url(self.path, fork),
            json=content,
            timeout=(60, 300),
        )

        self.send_response(response.status_code)
        self.send_header("Content-type", "application/octet-stream")
        self.end_headers()

        response_json = response.json()
        response_json["_info_metadata"] = {
            "eels-resolution": get_fork_resolution_info(fork),
        }
        self.wfile.write(json.dumps(response_json).encode("utf-8"))


class _UnixSocketHttpServer(socketserver.UnixStreamServer):
    last_response: Optional[float] = None
    processes: List[subprocess.Popen]

    def __init__(self, *args, **kwargs):
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self.running_daemons = set()
        self.processes = []
        self.lock = threading.Lock()
        super().__init__(*args, **kwargs)

    @staticmethod
    def get_subserver_url(path: str, fork: str):
        print("Getting subserver URL")
        socket_path = runtime_dir / (fork + "." + str(os.getpid()) + ".sock")
        quoted_str = quote(str(socket_path), safe="")

        parsed = urlparse(path)
        parsed = parsed._replace(scheme="http+unix", netloc=quoted_str)

        return urlunparse(parsed)

    def get_request(self) -> Tuple[Any, Any]:
        print("Getting request")
        request, client_address = super().get_request()
        return request, ["local", 0]

    def finish_request(
        self, request: Union[socket, Tuple[bytes, socket]], client_address: Any
    ) -> None:
        print("Finishing request")
        try:
            super().finish_request(request, client_address)
        finally:
            self.last_response = time.monotonic()

    def check_timeout(self) -> None:
        print("Starting timeout check thread")
        while True:
            time.sleep(11.0)
            now = time.monotonic()
            last_response = self.last_response
            if last_response is None:
                self.last_response = now
            elif now - last_response > 60.0:
                self.shutdown()
                break

    def spawn_subserver(self, fork):
        print(f"Spawning subserver for {fork}")
        with self.lock:
            if fork not in self.running_daemons:
                get_fork_resolution(fork).resolve(fork)

                uds_path = runtime_dir / (fork + "." + str(os.getpid()) + ".sock")
                self.processes.append(
                    subprocess.Popen(
                        args=[
                            sys.argv[0],
                            "spawn-daemon",
                            "--state.fork",
                            fork,
                            "--uds",
                            str(uds_path),
                            "--timeout=0",
                        ]
                    )
                )
                self.running_daemons.add(fork)
                wait_time = 0.1
                while not uds_path.exists():
                    wait_time *= 2
                    time.sleep(wait_time)
                    if wait_time > 100:
                        raise Exception(
                            "Sub-daemon taking excessively long to open unix socket"
                        )
                while True:
                    try:
                        Session().get(self.get_subserver_url("/heartbeat/", fork))
                        break
                    except ConnectionError:
                        time.sleep(wait_time)
                        wait_time *= 2

    def kill_subprocesses(self):
        print("Killing subprocesses")
        for process in self.processes:
            process.terminate()
        sleep(1)
        for process in self.processes:
            process.kill()


class Daemon:
    """
    Converts HTTP requests into ethereum-spec-evm calls.
    """

    def __init__(self, uds) -> None:
        self.uds = uds

    def _run(self) -> int:
        # Perform cleanup when receiving SIGTERM
        print(1)
        signal.signal(signal.SIGTERM, lambda x, y: sys.exit())
        print(2)

        try:
            os.remove(self.uds)
        except IOError:
            pass
        
        print(3)

        with _UnixSocketHttpServer(self.uds, _EvmToolHandler) as server:
            print(4)
            server.timeout = 7.0
            timer = Thread(target=server.check_timeout, daemon=True)
            timer.start()
            print(5)

            try:
                server.serve_forever()
                print(6)
            finally:
                server.kill_subprocesses()
                print(7)

        return 0

    def run(self) -> int:
        """
        Execute the tool.
        """
        return self._run()
