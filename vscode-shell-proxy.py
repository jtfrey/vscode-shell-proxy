#!/usr/bin/env python3
#
# This script acts as a proxy for the Microsoft Visual Studio Code application's
# Remote-SSH extension.  Remote-SSH must be setup to allow for remote command to
# be included in host configurations; the RemoteCommand is set to this script
# with various options permissible.
#
# Slurm is used to start an interactive shell on a compute node.  The stdio channels
# for that remote shell are proxied by this script to the stdio channels of the ssh
# session that executed this script.  In essence, i/o to/from the VSCode application
# flow through this script to the remote shell.
#
# Part of that proxy is watching the remote shell's stdout for notification of the
# TCP socket (port number) that the remote vscode software is using for control
# communications.  A TCP listener is opened by this script and its port substituted
# in that output.  The VSCode application receives the port number on the login node
# and communicates with that; this script accepts connections on that port and proxies
# them to the actual TCP listener on the compute node.
#

import argparse
import asyncio
import getpass
import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import Iterable
from typing import TextIO
from uuid import uuid4

if TYPE_CHECKING:
    from typing import List
    from typing import Tuple

    if sys.version_info >= (3, 10):
        from types import EllipsisType
    else:
        EllipsisType = Any

# === Helpers and Compatibility code ==========================================

if sys.version_info >= (3, 9):
    str_removeprefix = str.removeprefix

else:

    def str_removeprefix(__string: str, __prefix: str):
        if __string.startswith(__prefix):
            return __string[len(__prefix) :]
        return __string


if sys.version_info >= (3, 7):
    asyncio_create_task = asyncio.create_task
    asyncio_run = asyncio.run

else:
    asyncio_create_task = asyncio.ensure_future

    def asyncio_run(coro):
        loop = asyncio.get_event_loop()
        if loop.is_running():
            raise RuntimeError("Event loop is already running")
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _d(msg, *_args):
    logging.debug(msg, *(x.strip() if isinstance(x, str) else x for x in _args))


def _w(msg, *_args):
    logging.warning(msg, *(x.strip() if isinstance(x, str) else x for x in _args))


def _i(msg, *_args):
    logging.info(msg, *(x.strip() if isinstance(x, str) else x for x in _args))


# --- proxy states ---


class ProxyStates(Enum):
    # The script progresses through these states
    LAUNCH = 0
    BEGIN = 1
    START_PROXY = 2
    PROXY_STARTED = 3
    END = 4


class VSCodeTCPProxy:
    """tcp proxy for communication with vscode server

    This proxy runs on the headnode in this Python process
    and listens on the <listen_host>:<listen_port>.

    It transfers all communication to and from the
    <target_host>:<target_port> which is established once
    the VSCodeSubprocessIOTransform launched a slurm job,
    and determines the host and port that the vscode server
    started on.

    """

    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        backlog: int = 8,
        byte_limit: int = 4096,
        loop=None,
    ):
        # configuration
        self._listen_host = str(listen_host)
        self._listen_port = int(listen_port)
        self._backlog = int(backlog)
        self._byte_limit = int(byte_limit)
        self._loop = loop or asyncio.new_event_loop()
        # populated by tcp proxy
        #   This is the local TCP port on which this script is listening
        self.port: "int | None" = None
        # populated from outside
        #   This is the remote (compute node) hostname and TCP port
        #   on which vscode backend is listening:
        self.target_host: "str | None" = None
        self.target_port: "int | None" = None
        # threading states
        self.state: ProxyStates = ProxyStates.LAUNCH
        self.state_condition = threading.Condition()
        self._thread = threading.Thread(
            name="vscode-tcp-proxy",
            target=self._start_tcp_proxy,
            args=(self._loop,),
            daemon=True,
        )

    def start(self):
        self.set_state(ProxyStates.BEGIN, msg="main runloop")
        self._thread.start()

    async def stop(self):
        _d("Terminating TCP proxy event loop...")
        await self._loop.shutdown_asyncgens()
        self._loop.stop()
        _d("Proxy has terminated.")

    def set_state(self, state: ProxyStates, msg: str):
        with self.state_condition:
            self.state = state
            _d(f"[STATE] {state} <- {msg}")
            self.state_condition.notify_all()

    def wait_for(self, state: ProxyStates, msg: str):
        with self.state_condition:
            _d(msg)
            self.state_condition.wait_for(lambda: self.state == state)

    @property
    def target_addr(self) -> "Tuple[str, int] | None":
        if self.target_host is None or self.target_port is None:
            return None
        else:
            return self.target_host, self.target_port

    def __repr__(self):
        return (
            f"<{type(self).__name__}"
            f" state={self.state!r}"
            f" listen={self._listen_host}:{self._listen_port}"
            f" target={self.target_host}:{self.target_port}"
            ">"
        )

    # --- tcp proxy related methods -------------------------------------------

    def _start_tcp_proxy(self, loop):
        """Alternative thread that will run the vscode backend TCP port proxy runloop."""
        asyncio.set_event_loop(loop)
        _d("  Entering tcp proxy event loop")
        loop.run_until_complete(self._tcp_proxy())
        _d("  Exited tcp proxy event loop")

    async def _tcp_proxy(self):
        """The vscode backend TCP port proxy server."""
        with self.state_condition:
            _d("    Waiting on TCP proxy server start condition...")
            self.state_condition.wait_for(lambda: self.state == ProxyStates.START_PROXY)

            _d("    Starting TCP proxy server on port %d", self._listen_port)
            server = await asyncio.start_server(
                self._tcp_proxy_connect,
                self._listen_host,
                self._listen_port,
                reuse_port=True,
                backlog=self._backlog,
            )

            # Get the port we're listening on:
            self.port = server.sockets[0].getsockname()[1]

            _i("    Running TCP proxy server on port %d...", self.port)
            self.state = ProxyStates.PROXY_STARTED

            _d("[STATE] PROXY_STARTED <- TCP proxy runloop")
            self.state_condition.notify_all()

        async with server:
            await server.serve_forever()

        _d("    Terminating TCP proxy server.")

    async def _tcp_proxy_connect(
        self, src_reader: asyncio.StreamReader, src_writer: asyncio.StreamWriter
    ):
        """Accept a connection opened on the proxy port.
        Open a connection to the vscode backend then create two async transfer functions to forward data between them.
        """
        _addr = src_writer.get_extra_info("peername")
        _i("%s:%d connection accepted", *_addr)

        if self.target_host is None or self.target_port is None:
            raise RuntimeError("target_host or target_port not set")

        try:
            dst_reader, dst_writer = await asyncio.open_connection(
                self.target_host, self.target_port
            )
            t0 = asyncio_create_task(self._tcp_proxy_transfer(src_reader, dst_writer))
            t1 = asyncio_create_task(self._tcp_proxy_transfer(dst_reader, src_writer))
            _d("%s:%d i/o tasks scheduled", *_addr)
            await asyncio.gather(t0, t1)
            _d("%s:%d i/o tasks completed", *_addr)
        except Exception as err:
            logging.error("%s:%d failure: %s", *_addr, str(err))
            src_writer.close()

    async def _tcp_proxy_transfer(self, reader, writer):
        """Receive data from a reader and send it on a writer,
        closing and exiting from this function on any errors, end-of-file, etc."""

        while not reader.at_eof() and not writer.is_closing():
            data = await reader.read(self._byte_limit)
            if data:
                # Send the data on the writer:
                writer.write(data)
                await writer.drain()
            else:
                # No data implies the reader has closed:
                writer.close()
                break


class VSCodeSubprocessIOTransform:
    """run a command on a slurm node and connect to the process's stdin/stdout/stderr

    This class is used to spawn the vscode server on a slurm node and
    to do some setup on the node, as well as parse certain outputs from
    the nodes stdout (like for example the target port of the vscode server)

    """

    # Any commands this script itself sends to the remote shell should have
    # their output prefixed with this text to indicate they are NOT in response
    # to VSCode application commands:
    SHELL_OUTPUT_PREFIX = "VSCODE_SHELL_PROXY::::"

    def __init__(
        self,
        cmd: "List[str]",
    ):
        self._cmd = cmd
        self._proc: "subprocess.Popen | None" = None
        self._threads: "List[threading.Thread]" = []
        # additional _proc env
        _short_uuid = hashlib.sha256(str(uuid4()).encode()).hexdigest()[:16]
        run_dir = Path.home().joinpath(".run", _short_uuid)
        run_dir.mkdir(exist_ok=True, parents=True)
        self._proc_env = {
            **os.environ,
            # store the head node's ssh_auth_sock location
            "LOGIN_SSH_AUTH_SOCK": os.environ.get("SSH_AUTH_SOCK", ""),
            # set the runtime dir on the node to a writable location unique to the job
            "XDG_RUNTIME_DIR": str(run_dir.absolute()),
        }

    def start(self, proxy: VSCodeTCPProxy):
        assert self._proc is None, "start only once"
        _d('Command to launch remote shell: "%s"', " ".join(self._cmd))
        self._proc = proc = subprocess.Popen(
            self._cmd,
            env=self._proc_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,  # equal text=True
            bufsize=1,  # activates line buffering in text mode
        )
        _i("Remote shell launched with pid %d", self._proc.pid)

        # Start the stdio threads:
        self._threads[:] = [
            threading.Thread(name=name, target=target, args=_args, daemon=True)
            for name, target, _args in [
                (
                    "remote-stdin",
                    self._stdin_proxy_func,
                    (sys.stdin, proc.stdin, proxy),
                ),
                (
                    "remote-stdout",
                    self._stdout_proxy_func,
                    (proc.stdout, sys.stdout, proxy),
                ),
                (
                    "remote-stderr",
                    self._stderr_proxy_func,
                    (proc.stderr, sys.stderr, proxy),
                ),
            ]
        ]
        for t in self._threads:
            t.start()

    def stop(self):
        _i("Terminating remote shell process...")
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            self._proc.kill()

    # --- io transform and extraction functions -------------------------------

    # Regex for input lines containing references to running servers bound to the localhost interface only:
    RE_NODEJS_LOCALHOST = re.compile(
        r"((\$args =.*)|(\$VSCH_SERVER_SCRIPT.*))--host=127.0.0.1"
    )
    RE_CLI_LISTENARGS_LOCALHOST = re.compile(
        r'(LISTEN_ARGS=".*)(--on-host=(([0-9]+\.){3}[0-9]+))'
    )
    RE_CLI_CMD_LOCALHOST = re.compile(
        r"(VSCODE_CLI_REQUIRE_TOKEN=[0-9a-fA-F-]*.*\$CLI_PATH.*command-shell )(.*)(--on-host=(([0-9]+\.){3}[0-9]+))"
    )

    @classmethod
    def _stdin_proxy_func(cls, src: TextIO, dst: TextIO, proxy: VSCodeTCPProxy):
        # Start by sending our special `hostname` command:
        cmd = f'echo "{cls.SHELL_OUTPUT_PREFIX}HOSTNAME=$(hostname)"\n'
        dst.write(cmd)
        dst.flush()
        _d("Wrote startup command to remote shell: %s", cmd)

        # And now setup ssh-agent forwarding from the headnode to the node
        ssh_fwd_cmd = (
            "ssh -q -NfL ~/.ssh/$(hostname).sock:$LOGIN_SSH_AUTH_SOCK"
            ' -o "StreamLocalBindUnlink=yes" -o "ExitOnForwardFailure=yes"'
            " $SLURM_SUBMIT_HOST"
            " && export SSH_AUTH_SOCK=~/.ssh/$(hostname).sock"
            " && ssh-add -L > /dev/null 2>&1\n"
        )
        dst.write(ssh_fwd_cmd)
        dst.flush()
        _d("Wrote SSH_AUTH_SOCK forwarding command to remote shell: %s", ssh_fwd_cmd)

        # state for the loop below
        _has_cli_listen_args = False

        for line in iter(src.readline, ""):
            m: "re.Match[str] | EllipsisType | None" = ...

            # fix localhost (check if these can be disabled at some stage)
            if "--host=127.0.0.1" in line:
                m = cls.RE_NODEJS_LOCALHOST.search(line)
                if m:
                    _d("localhost line found NODEJS: %s", line)
                    line = line.replace("127.0.0.1", "0.0.0.0")

            elif not _has_cli_listen_args and '"$CLI_PATH" command-shell' in line:
                m = cls.RE_CLI_CMD_LOCALHOST.search(line)
                if m:
                    _d("localhost line found CLI_CMD: %s", line)
                    line = cls.RE_CLI_CMD_LOCALHOST.sub(
                        r"\g<1> --on-host=0.0.0.0 \g<2>", line
                    )

            elif "LISTEN_ARGS=" in line:
                m = cls.RE_CLI_LISTENARGS_LOCALHOST.search(line)
                if m:
                    _d("localhost line found CLI_LISTENARGS: %s", line)
                    line = cls.RE_CLI_LISTENARGS_LOCALHOST.sub(
                        r"\g<1> --on-host=0.0.0.0 ", line
                    )
                    _has_cli_listen_args = True

            if m is None:
                _w("unanticipated localhost line found: %s", line)
            elif m is not ...:
                _d("localhost line found and fixed: %s", line)

            dst.write(line)
            dst.flush()

        # All done, let everyone know:
        proxy.set_state(ProxyStates.END, msg="stdin thread")

    # Regex for the line output by the vscode backend indicating the TCP port on which it is listening:
    RE_TARGET_PORT = re.compile(
        r"^([^0-9]*listeningOn=[^0-9]*)(([0-9]+\.){3}[0-9]+:)?([0-9]+)([^0-9]*)$"
    )

    @classmethod
    def _stdout_proxy_func(cls, src: TextIO, _: TextIO, proxy: VSCodeTCPProxy):
        """Consume output to the remote shell's stdout and write it to this script's stdout.

        This function is far more complex compared to the stderrProxyThread() function:

        the stdout lines must be scanned for output associated with commands issued by this script
        (e.g. to get the remote hostname) and the remote TCP port on which the vscode backend is
        listening.  Once those data are known, this script's TCP proxy can be started. When EOF is
        reached on the remote shell's stdout the state is forwarded to END, yielding the shutdown
        of this script -- the connection to the remote shell has been severed.

        """
        _listen_on_had_host = False
        _target_port_line: "str | None" = None

        for line in iter(src.readline, ""):
            omit = False  # forward the line to the destination

            # Check if it's an injected command
            if line.startswith(cls.SHELL_OUTPUT_PREFIX):
                line = str_removeprefix(line, cls.SHELL_OUTPUT_PREFIX)

                if line.startswith("HOSTNAME="):
                    proxy.target_host = th = str_removeprefix(line, "HOSTNAME=").strip()
                    _i("Remote hostname found:  %s", th)

                omit = True

            # Check if it contains the target_port
            elif proxy.target_port is None and "listeningOn=" in line:
                _d("Remote vscode TCP listener port found: %s", line)

                m = cls.RE_TARGET_PORT.search(line)
                if m:
                    proxy.target_port = tp = int(m.group(4))
                    _i("Remote TCP port found: %d", tp)

                    if m.group(2):
                        _listen_on_had_host = m.group(2) != ""

                # Don't print the line now, stash it for output once the TCP proxy
                # has started:
                omit = True
                _target_port_line = line

            if (
                proxy.state == ProxyStates.BEGIN
                and proxy.target_addr is not None
                and _target_port_line is not None
            ):
                # Before going any further, start the proxy:
                proxy.set_state(ProxyStates.START_PROXY, msg="stdout thread")
                # Once it's started we can continue:
                proxy.wait_for(
                    ProxyStates.PROXY_STARTED,
                    msg="stdout waiting for TCP Proxy startup...",
                )

                # Reformat the line with the local listening port:
                _target_host_str = "127.0.0.1:" if _listen_on_had_host else ""
                _target_port_line = cls.RE_TARGET_PORT.sub(
                    rf"\g<1>{_target_host_str:s}{proxy.port:d}\g<5>",
                    _target_port_line,
                )
                _d("Remote vscode TCP listener line rewritten: %s", _target_port_line)
                sys.stdout.write(_target_port_line)

            if not omit:
                sys.stdout.write(line)
            sys.stdout.flush()

        # All done, let everyone know:
        proxy.set_state(ProxyStates.END, msg="stdout thread")

    @classmethod
    def _stderr_proxy_func(cls, src: TextIO, dst: TextIO, _: VSCodeTCPProxy):
        """Consume output to the remote shell's stderr and write it to this script's stderr."""
        for line in iter(src.readline, ""):
            dst.write(line)
            dst.flush()


async def runloop(
    listen_host: str,
    listen_port: int,
    backlog: int = 8,
    byte_limit: int = 4096,
    salloc_args: "Iterable[str]" = (),
):
    """The main asyncio event loop for this script.
    Starts the TCP port proxy so it will be awaiting a startup signal
    (once the remote hostname and TCP port are known).
    Launches the remote shell with Slurm `salloc` and connects its stdio channels to threaded i/o handlers.
    The function then goes to sleep until the program state reaches END, then cleans-up the remote shell
    subprocess and TCP proxy runloop before exiting.
    """

    vscode_proxy = VSCodeTCPProxy(
        listen_host,
        listen_port,
        backlog,
        byte_limit,
        loop=asyncio.new_event_loop(),
    )
    vscode_proxy.start()

    # gather parameters for the remote shell command
    user = (getpass.getuser() or "unknown").upper()

    cmd = [
        "srun",
        "--ntasks=1",
        "--cpus-per-task=4",
        "--mem-per-cpu=4G",
        f"--job-name=vscode-proxy-{user}",
        "--qos=interactive",
        "--time=02:00:00",
        "--nodes=1",
        *salloc_args,
    ]
    cmd += ["/bin/bash"]

    cmd_proc = VSCodeSubprocessIOTransform(cmd=cmd)
    cmd_proc.start(proxy=vscode_proxy)

    # block main thread progression until we stop the proxy
    vscode_proxy.wait_for(ProxyStates.END, msg="Awaiting proxy termination")

    # Terminate the remote shell:
    cmd_proc.stop()

    await vscode_proxy.stop()


def parse_cli():
    parser = argparse.ArgumentParser(description="vscode remote shell proxy")
    parser.add_argument(
        "-v",
        "--verbose",
        default=0,
        action="count",
        help="increase the level of output as the program executes",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        default=0,
        action="count",
        help="decrease the level of output as the program executes",
    )
    parser.add_argument(
        "-b",
        "--backlog",
        metavar="<N>",
        default=8,
        type=int,
        help="number of backlogged connections held by the proxy socket (see man page for listen())",
    )
    parser.add_argument(
        "-B",
        "--byte-limit",
        metavar="<N>",
        default=4096,
        type=int,
        help="maximum bytes read at one time per socket",
    )
    parser.add_argument(
        "-H",
        "--listen-host",
        metavar="<HOSTNAME>",
        default="127.0.0.1",
        help="the client-facing TCP proxy should bind to this interface (default 127.0.0.1; use 0.0.0.0 for all interfaces)",
    )
    parser.add_argument(
        "-p",
        "--listen-port",
        metavar="<N>",
        default=0,
        type=int,
        help="the client-facing TCP proxy port (default 0 implies a random port is chosen)",
    )
    parser.add_argument(
        "-S",
        "--salloc-arg",
        metavar="<SLURM-ARG>",
        action="append",
        help="used zero or more times to specify arguments to the salloc command being wrapped (e.g. --partition=<name>, --ntasks=<N>)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    # parse the cli arguments
    args = parse_cli()

    # logging
    _idx = min(max(0, 1 + args.verbose - args.quiet), 4)
    level = ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"][_idx]
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")

    # run the proxy
    asyncio_run(
        runloop(
            listen_host=args.listen_host,
            listen_port=args.listen_port,
            backlog=args.backlog,
            byte_limit=args.byte_limit,
            salloc_args=args.salloc_arg or (),
        )
    )
