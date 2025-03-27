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

import logging
import argparse
import threading
import sys
import os
import ast
import subprocess
import re
from enum import Enum

#
# The following are used for type-checking throughout the code; some of the
# types are handled differently across 3.x versions.
#
from typing import TYPE_CHECKING, Any, TextIO, BinaryIO
EllipsisType = Any
if sys.version_info >= (3, 9):
    from collections.abc import Iterable
    from builtins import list as List
    from builtins import tuple as Tuple
    if sys.version_info >= (3, 10):
        from types import EllipsisType
else:
    from typing import List, Tuple, Iterable

#
# Abstract-out some functions that creates an async task and execute
# it to account for API differences from 3.7 onward:
#
import asyncio
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

#
# Define a version-agnostic string prefix removal function:
#
if sys.version_info >= (3, 9):
    str_removeprefix = str.removeprefix
else:
    def str_removeprefix(__string: str, __prefix: str):
        if __string.startswith(__prefix):
            return __string[len(__prefix) :]
        return __string


class ProxyStates(Enum):
    """The states that the proxy moves through as the proxy executes."""
    LAUNCH = 0
    BEGIN = 1
    START_PROXY = 2
    PROXY_STARTED = 3
    END = 4


class ProxyDirection(Enum):
    """The proxy directionality."""
    FORWARD = 0
    REVERSE = 1


class VSCodeProxyConfig(argparse.Namespace):
    """The VSCodeProxyConfig class is designed to include all configurable/modifiable attributes of this program.  It represents the baseline functionality and site-specific modifications can be implemented by subclassing in a script file adjacent to this script on the file system.

Subclasses should:

- Overload the cli_parser() class method to chain to the parent class's method and augment the returned argparse object before returning it to the caller.
- Overload the set_defaults() instance method to chain to the parent class's method and provide default values to its own instance variables.
"""

    SHELL_OUTPUT_PREFIX: str = 'VSCODE_SHELL_PROXY::::'
    LOGGING_LEVELS: List[int] = [ logging.CRITICAL,
                       logging.ERROR,
                       logging.WARNING,
                       logging.INFO,
                       logging.DEBUG ]
    BASE_LOGGING_LEVEL: int = 1 # logging.ERROR

    DEFAULT_VERBOSITY: int = 0
    DEFAULT_QUIETNESS: int = 0
    DEFAULT_LISTEN_PORT: int = 0
    DEFAULT_LISTEN_HOST: str = '127.0.0.1'
    DEFAULT_CONN_BACKLOG: int = 8
    DEFAULT_READ_BYTE_LIMIT: int = 4096
    
    DEFAULT_SUBPROCESS_WAIT_TIMEOUT: int = 10
    
    SCHEDULER_NAME: str = 'local-shell'
    
    @staticmethod
    def load_local_script(global_ns=None, local_ns=None) -> 'str|None':
        """Turn the path and filename of this script into a site-specific adjacent config file.  The string '-local' is added to the filename, e.g. 'script.py' => 'script-local.py'."""
        our_fname = os.path.splitext(os.path.basename(__file__))
        local_cfg_script = os.path.join(os.path.dirname(__file__), our_fname[0] + '-local' + our_fname[1])
        if os.path.isfile(local_cfg_script):
            try:
                with open(local_cfg_script, 'r') as fptr:
                    local_cfg_src = fptr.read()
            except Exception as err:
                sys.stderr.write('ERROR:  unable to open local proxy config script `{:s}'' for reading: {:s}\n'.format(local_cfg_script, str(err)))
                sys.exit(-1)
            try:
                local_cfg_code = compile(local_cfg_src, local_cfg_script, 'exec')
                exec(local_cfg_code, global_ns, local_ns)
                return local_cfg_script
            except Exception as err:
                import traceback
                sys.stderr.write('ERROR:  unable to execute local proxy config script:  {:s}\n'.format(str(err)))
                traceback.print_tb(err.__traceback__, file=sys.stderr)
                sys.exit(-1)
        return None
    
    @classmethod
    def cli_parser(cls):
        """The cli_parser() class method creates and returns an argparse instance that can parse command-line arguments for the proxy.  Site-specific extensions can be added by subclassing VSCodeProxyConfig and supplying a cli_parser() class method that chains to its parent class's method and uses add_argument() to augment the parser returned by the parent method."""
        cli_parser = argparse.ArgumentParser(description='vscode remote shell proxy')
        cli_parser.add_argument('-v', '--verbose',
                dest='verbosity',
                default=cls.DEFAULT_VERBOSITY,
                action='count',
                help='increase the level of output as the program executes')
        cli_parser.add_argument('-q', '--quiet',
                dest='quietness',
                default=cls.DEFAULT_QUIETNESS,
                action='count',
                help='decrease the level of output as the program executes')
        cli_parser.add_argument('-l', '--log-file', metavar='<PATH>',
                dest='log_file',
                default=None,
                help='direct all logging to this file rather than stderr; the token "[PID]" will be replaced with the running pid')
        cli_parser.add_argument('-0', '--tee-stdin', metavar='<PATH>',
                dest='tee_stdin_file',
                default=None,
                help='send a copy of input to the script stdin to this file; the token "[PID]" will be replaced with the running pid')
        cli_parser.add_argument('-1', '--tee-stdout', metavar='<PATH>',
                dest='tee_stdout_file',
                default=None,
                help='send a copy of output to the script stdout to this file; the token "[PID]" will be replaced with the running pid')
        cli_parser.add_argument('-2', '--tee-stderr', metavar='<PATH>',
                dest='tee_stderr_file',
                default=None,
                help='send a copy of output to the script stderr to this file; the token "[PID]" will be replaced with the running pid')
        cli_parser.add_argument('-b', '--backlog', metavar='<N>',
                dest='conn_backlog',
                default=cls.DEFAULT_CONN_BACKLOG,
                type=int,
                help='number of backlogged connections held by the proxy socket (see man page for listen(), default {:d})'.format(cls.DEFAULT_CONN_BACKLOG))
        cli_parser.add_argument('-B', '--byte-limit', metavar='<N>',
                dest='read_byte_limit',
                default=cls.DEFAULT_READ_BYTE_LIMIT,
                type=int,
                help='maximum bytes read at one time per socket (default {:d})'.format(cls.DEFAULT_READ_BYTE_LIMIT))
        cli_parser.add_argument('-H', '--listen-host', metavar='<HOSTNAME>',
                dest='listen_host',
                default=cls.DEFAULT_LISTEN_HOST,
                help='the client-facing TCP proxy should bind to this interface (default {:s}; 0.0.0.0 => all interfaces)'.format(cls.DEFAULT_LISTEN_HOST))
        cli_parser.add_argument('-p', '--listen-port', metavar='<N>',
                dest='listen_port',
                default=cls.DEFAULT_LISTEN_PORT,
                type=int,
                help='the client-facing TCP proxy port (default {:d}; 0 => random port)'.format(cls.DEFAULT_LISTEN_PORT))
        cli_parser.add_argument('-S', '--{:s}-arg'.format(cls.SCHEDULER_NAME), metavar='<{:s}-ARG>'.format(cls.SCHEDULER_NAME.upper()),
                dest='scheduler_args',
                action='append',
                help='used zero or more times to specify arguments to the {:s} command that launches the backend'.format(cls.SCHEDULER_NAME))
        return cli_parser
    
    @classmethod
    def create_with_cli_args(cls):
        """The create_with_cli_args() class method creates and returns an instance initialized with command-line arguments provided by the argparse parser returned by the cli_parser() class method.  Subclasses may provide their own cli_parser() and set_defaults() methods, but this one should not require replacement."""
        cli_parser = cls.cli_parser()
        cli_args = cli_parser.parse_args()
        return cls(**cli_args.__dict__)

    def __init__(self, **kwargs):
        """Initialize an instance of the class with default values augmented by keyword arguments (possibly coming from an argparse call)."""
        self.set_defaults()
        # Allow CLI arguments to override defaults:
        super(VSCodeProxyConfig, self).__init__(**kwargs)\
    
    def set_defaults(self):
        """Supply default values to instance variables in the receiver."""
        self.shell_output_prefix: str = self.__class__.SHELL_OUTPUT_PREFIX
        self.verbosity: int = self.__class__.DEFAULT_VERBOSITY
        self.quietness: int = self.__class__.DEFAULT_QUIETNESS
        self.log_file: 'str|None' = None
        self.tee_stdin_file: 'str|None' = None
        self.tee_stdout_file: 'str|None' = None
        self.tee_stderr_file: 'str|None' = None
        self.conn_backlog: int = self.__class__.DEFAULT_CONN_BACKLOG
        self.read_byte_limit: int = self.__class__.DEFAULT_READ_BYTE_LIMIT
        self.listen_port: int = self.__class__.DEFAULT_LISTEN_PORT
        self.listen_host: str = self.__class__.DEFAULT_LISTEN_HOST
        self.salloc_args: 'Iterable[str]|None' = None
    
    def init_logging(self):
        """Given the parameters associated with the receiver, initialize the default Python logger facility.  Returns self so that this method can be chained with others."""
        # Calculate the desired logging level...
        chosen_logging_level = self.__class__.BASE_LOGGING_LEVEL + self.verbosity - self.quietness
        # ...and limit to our range of levels:
        chosen_logging_level = min(max(0, chosen_logging_level), len(self.__class__.LOGGING_LEVELS)-1)
        if self.log_file:
            # Replace tokens in the log file name:
            self.log_file = self.log_file.replace('[PID]', str(os.getpid()))
        # Configure the logging class:
        logging.basicConfig(filename=self.log_file,
                            level=self.__class__.LOGGING_LEVELS[chosen_logging_level],
                            format='%(asctime)s [%(levelname)s] %(message)s')
        return self
    
#
# Logging helpers:
#
def log_crit(msg, *vals):
    logging.critical(msg, *(v.strip() if isinstance(v, str) else v for v in vals))
def log_err(msg, *vals):
    logging.error(msg, *(v.strip() if isinstance(v, str) else v for v in vals))
def log_warn(msg, *vals):
    logging.warning(msg, *(v.strip() if isinstance(v, str) else v for v in vals))
def log_info(msg, *vals):
    logging.info(msg, *(v.strip() if isinstance(v, str) else v for v in vals))
def log_debug(msg, *vals):
    logging.debug(msg, *(v.strip() if isinstance(v, str) else v for v in vals))
        
# Set the default configuration class — a local proxy config script can alter this:
VSCodeProxyConfigClass = VSCodeProxyConfig



class VSCodeTCPProxy(object):
    """An instance of VSCodeTCPProxy forms the bridge between a local TCP port and the TCP listening port opened by the remote vscode extension on a compute node.

The instance listens on TCP <host>:<port> indicated by the VSCodeProxyConfig object passed to it.  In practice, the VSCode software on a client's computer will attempt to connect to <cfg.listen_host>:<cfg.listen_port> to establish and reestablish communications with the backend.  This class accepts that connection, opens a connection to the <backend_host>:<backend_port> on the compute node (on which the extension is backend), and then passes traffic back and forth between the two endpoints.
"""

    def __init__(self, cfg: VSCodeProxyConfig, runloop: 'asyncio.AbstractEventLoop|None'=None):
        # Retain a reference to the configuration:
        self._cfg = cfg
        
        # This is the remote (compute node) host id and TCP port on which vscode backend is listening:
        self.backend_host: "str|None" = None
        self.backend_port: "int|None" = None
        
        # State of the proxy:
        self.state: ProxyStates = ProxyStates.LAUNCH
        
        # Synchronize state changes between the proxy thread and the rest of the code using a
        # Condition object:
        self._state_condition = threading.Condition()
        
        # Use the existing runloop OR create a new one if none was provided:
        self._runloop: asyncio.AbstractEventLoop = runloop if runloop is not None else asyncio.new_event_loop()
        
        # Create a separate thread in which the proxy i/o will happen:
        self._thread = threading.Thread(
            name='vscode-tcp-proxy',
            target=self._start_tcp_proxy,
            args=(self._runloop,),
            daemon=True)

    def start(self):
        """Start the i/o thread associated with this proxy.  Returns self so that methods can be chained."""
        self.set_state(ProxyStates.BEGIN, msg='main runloop')
        self._thread.start()
        return self

    async def stop(self):
        """Attempt to stop the i/o thread associated with this proxy.  Returns self so that methods can be chained."""
        log_debug('Terminating TCP proxy event loop...')
        await self._runloop.shutdown_asyncgens()
        self._runloop.stop()
        log_debug('Proxy has terminated.')
        return self

    def set_state(self, state: ProxyStates, msg: str):
        """Change the state of the TCP proxy.  Returns self so that methods can be chained."""
        with self._state_condition:
            self.state = state
            log_debug(f'[STATE] {state} <- {msg}')
            self._state_condition.notify_all()
        return self

    def wait_for(self, state: ProxyStates, msg: str):
        """Wait for the TCP proxy to reach the given state.  Returns self so that methods can be chained."""
        with self._state_condition:
            log_debug(msg)
            self._state_condition.wait_for(lambda: self.state == state)
        return self

    @property
    def backend_addr(self) -> 'Tuple[str, int] | None':
        if self.backend_host is None or self.backend_port is None:
            return None
        else:
            return (self.backend_host, self.backend_port)

    def __repr__(self):
        return (
            f'<{type(self).__name__}'
            f' state={self.state!r}'
            f' listen={self._cfg.listen_host}:{self._cfg.listen_port}'
            f' target={self.backend_host}:{self.backend_port}'
            '>'
        )
    
    def _start_tcp_proxy(self, runloop: asyncio.AbstractEventLoop):
        """Thread entrypoint that will handle the backend TCP proxy."""
        asyncio.set_event_loop(runloop)
        
        log_debug('  Entering backend TCP proxy event loop',)
        runloop.run_until_complete(self._tcp_proxy_listen())
        log_debug('  Exited backend TCP proxy event loop',)
    
    async def _tcp_proxy_listen(self):
        """Once the backend TCP host:port are known, listen for connections from the client's computer to start proxying communications."""
        with self._state_condition:
            log_debug('    Waiting on backend TCP proxy server start condition...')
            self._state_condition.wait_for(lambda: self.state == ProxyStates.START_PROXY)
            
            self.notify_will_listen()

            log_info('    Starting backend TCP proxy server on port %d', self._cfg.listen_port)
            server = await asyncio.start_server(
                self._tcp_proxy_connect,
                self._cfg.listen_host,
                self._cfg.listen_port,
                reuse_port=True,
                backlog=self._cfg.conn_backlog)

            # "Publish" the TCP port on which we're listening:
            self.actual_listen_port = server.sockets[0].getsockname()[1]
            log_info('    Running backend TCP proxy server on port %d...', self.actual_listen_port)
            
            self.notify_is_listening()
            
            self.state = ProxyStates.PROXY_STARTED

            log_debug('[STATE] PROXY_STARTED <- TCP proxy runloop')
            self._state_condition.notify_all()
        async with server:
            await server.serve_forever()
        log_debug('    Terminating backend TCP proxy server.')
    
    def notify_will_listen(self):
        """A callback that can be implemented by subclasses to execute code before the proxy begins listening for connections."""
        pass
    def notify_is_listening(self):
        """A callback that can be implemented by subclasses to execute code after the proxy is listening for connections.  The state has not transitioned to PROXY_STARTED yet."""
        pass

    async def _tcp_proxy_connect(self, src_reader: asyncio.StreamReader, src_writer: asyncio.StreamWriter):
        """Accept a connection opened on the proxy port, open a connection to the backend on the compute node, then create two async transfer functions to forward data between them."""
        target_addr = src_writer.get_extra_info("peername")
        log_info('%s:%d connection accepted', *target_addr)
        self.notify_did_client_connect(*target_addr)

        if self.backend_host is None:
            raise RuntimeError('Backend TCP host is not set')
        if self.backend_port is None:
            raise RuntimeError('Backend TCP port is not set')

        try:
            self.notify_will_connect_backend(self.backend_host, self.backend_port)
            dst_reader, dst_writer = await asyncio.open_connection(self.backend_host, self.backend_port)
            self.notify_did_connect_backend(dst_reader, dst_writer)
            
            t_fwd = asyncio_create_task(self._tcp_proxy_transfer(ProxyDirection.FORWARD, src_reader, dst_writer))
            t_rev = asyncio_create_task(self._tcp_proxy_transfer(ProxyDirection.REVERSE, dst_reader, src_writer))
            self.notify_did_launch_io_streams(t_fwd, t_rev)
            log_debug('%s:%d i/o tasks scheduled', *target_addr)
            await asyncio.gather(t_fwd, t_rev)
            self.notify_did_complete_io_streams()
            log_debug('%s:%d i/o tasks completed', *target_addr)
        except Exception as err:
            log_err('%s:%d failure: %s', *target_addr, str(err))
            src_writer.close()
    
    def notify_did_client_connect(self, client_host: str, client_port: int):
        """A callback that can be implemented by subclasses to execute code after a client has connected to the TCP proxy."""
        pass
    def notify_will_connect_backend(self, backend_host: str, backend_port: int):
        """A callback that can be implemented by subclasses to execute code before the proxy connects to the backend."""
        pass
    def notify_did_connect_backend(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """A callback that can be implemented by subclasses to execute code after the proxy has connected to the backend but before the i/o streams have been established."""
        pass
    def notify_did_launch_io_streams(self, fwd_task, rev_task):
        """A callback that can be implemented by subclasses to execute code after the backend i/o streams have been established."""
        pass
    def notify_did_complete_io_streams(self):
        """A callback that can be implemented by subclasses to execute code after the backend i/o streams have closed."""
        pass

    async def _tcp_proxy_transfer(self, direction: ProxyDirection, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Receive data from a reader and send it on a writer,
        closing and exiting from this function on any errors, end-of-file, etc."""
        while not reader.at_eof() and not writer.is_closing():
            data = await reader.read(self._cfg.read_byte_limit)
            if data:
                self.notify_did_read_io_stream_data(direction, data)
                
                # Send the data on the writer:
                writer.write(data)
                await writer.drain()
                self.notify_did_write_io_stream_data(direction, data)
            else:
                # No data implies the reader has closed:
                writer.close()
                break
    def notify_did_read_io_stream_data(self, direction: ProxyDirection, data: bytes):
        """A callback that can be implemented by subclasses to execute code after a backend i/o stream has read data."""
        pass
    def notify_did_write_io_stream_data(self, direction: ProxyDirection, data: bytes):
        """A callback that can be implemented by subclasses to execute code after a backend i/o stream has written data."""
        pass
        
# Set the default TCP proxy class — a local proxy config script can alter this:
VSCodeTCPProxyClass = VSCodeTCPProxy



class VSCodeBackendLauncher(object):
    """Execute the vscode backend (likely on a compute node) and capture that process's stdin/stdout/stderr streams.  The stdout stream is monitored to extract the backend's TCP port for the sake of the binary TCP proxy."""
    
    def __init__(self, cfg: VSCodeProxyConfig):
        # Retain a reference to the configuration:
        self._cfg = cfg
        
        # Initialize instance variables:
        self._proxy_threads: 'List[threading.Thread]' = []
    
    def job_scheduler_base_cmd(self):
        """Return the baseline command array that is used to interactively start the vscode backend.  By default, the `bash` command is returned, which would start a local shell in which the vscode backend starts.

Subclasses should override this method to return site-specific mechanisms.  For example, Slurm `salloc` with some standard options might return ['salloc', '--ntasks=1', '--partition=bigmem'].  Or for PBS, an interactive job would use ['qsub', '-I'].
"""
        return ['bash']
        
    def get_job_scheduler_cmd(self) -> List[str]:
        """Augments the baseline command with additional arguments provided by the config object."""
        cmd = self.job_scheduler_base_cmd()
        addl_args = self._cfg.scheduler_args
        if addl_args:
            cmd.extend(addl_args)
        return cmd
        
    def get_job_env(self) -> dict:
        """Returns a dictionary containing the key-value pairs that should be present in the backend subprocess's environment."""
        return { **os.environ }

    def start(self, proxy: VSCodeTCPProxy):
        """Start the backend process and plumb its i/o proxy threads.  Returns self so that methods can be chained."""
        if '_job_proc' in self.__dict__:
            log_crit('Attempt to start job submission wrapper multiple times')
            sys.exit(-1)
        job_cmd = self.get_job_scheduler_cmd()
        log_debug('Command to launch vscode backend: %s', ' '.join(job_cmd))
        job_env = self.get_job_env()
        self.notify_will_launch_backend(cmd=job_cmd, env=job_env)
        self._job_proc = subprocess.Popen(
                job_cmd,
                env=job_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,  # equal text=True
                bufsize=1,  # activates line buffering in text mode
            )
        log_info('Launched vscode backend with pid %d', self._job_proc.pid)
        self.notify_did_launch_backend(self._job_proc)
        
        # Create the stdio monitoring threads:
        self._proxy_threads[:] = [
            threading.Thread(name=thread_name, target=thread_target, args=thread_args, daemon=True)
            for thread_name, thread_target, thread_args in [
                (
                    "remote-stdin",
                    self._stdin_proxy_fn,
                    (sys.stdin, self._job_proc.stdin, proxy),
                ),
                (
                    "remote-stdout",
                    self._stdout_proxy_fn,
                    (self._job_proc.stdout, sys.stdout, proxy),
                ),
                (
                    "remote-stderr",
                    self._stderr_proxy_fn,
                    (self._job_proc.stderr, sys.stderr, proxy),
                )
            ]
        ]
        self.notify_did_create_stdio_threads(*self._proxy_threads)
        for t in self._proxy_threads:
            t.start()
        self.notify_did_start_stdio_threads(*self._proxy_threads)
        
        return self
    
    def notify_will_launch_backend(self, cmd: Iterable[str], env: dict):
        """A callback that can be implemented by subclasses to execute code immediately before the backend process is launched."""
        pass
    def notify_did_launch_backend(self, job_proc: 'subprocess.Popen'):
        """A callback that can be implemented by subclasses to execute code after the backend process has been launched but before stdio monitoring threads have been created."""
        pass
    def notify_did_create_stdio_threads(self, stdin_thread, stdout_thread, stderr_thread):
        """A callback that can be implemented by subclasses to execute code after the stdio monitoring threads have been created."""
        pass
    def notify_did_start_stdio_threads(self, stdin_thread, stdout_thread, stderr_thread):
        """A callback that can be implemented by subclasses to execute code after the stdio monitoring threads have been started."""
        pass

    def stop(self):
        """Stop the backend process and shutdown its i/o proxy threads.  Returns self so that methods can be chained."""
        if '_job_proc' in self.__dict__:
            log_info('Terminating vscode backend process...')
            self.notify_will_terminate_backend(self._job_proc)
            self._job_proc.terminate()
            try:
                self._job_proc.wait(timeout=self._cfg.__class__.DEFAULT_SUBPROCESS_WAIT_TIMEOUT)
            except (OSError, subprocess.SubprocessError):
                self._job_proc.kill()
            self.notify_did_terminate_backend(self._job_proc)
        return self
    
    def notify_will_terminate_backend(self, job_proc: 'subprocess.Popen'):
        """A callback that can be implemented by subclasses to execute code immediately before the backend process is terminated."""
        pass
    def notify_did_terminate_backend(self, job_proc: 'subprocess.Popen'):
        """A callback that can be implemented by subclasses to execute code immediately before the backend process is terminated."""
        pass
        
    # Regex for input lines containing references to running servers bound to the localhost interface only:
    RE_NODEJS_LOCALHOST = re.compile(
            r'((\$args =.*)|(\$VSCH_SERVER_SCRIPT.*))--host=127.0.0.1')
    RE_CLI_LISTENARGS_LOCALHOST = re.compile(
            r'(LISTEN_ARGS=".*)(--on-host=(([0-9]+\.){3}[0-9]+))')
    RE_CLI_CMD_LOCALHOST = re.compile(
            r'(VSCODE_CLI_REQUIRE_TOKEN=[0-9a-fA-F-]*.*\$CLI_PATH.*command-shell )(.*)(--on-host=(([0-9]+\.){3}[0-9]+))')
    
    def _stdin_proxy_fn(self, src: TextIO, dst: TextIO, proxy: VSCodeTCPProxy):
        """Consume input coming from the client and proxy it to the backend process's stdin.  Commands that provide CLI options or variable values that attempt to confine the backend to opening its binary TCP port on localhost are intercepted and altered.  If a stdin tee file was requested by the user, then all lines are mirrored to that file."""
        # Check the config for a stdin tee file:
        if self._cfg.tee_stdin_file:
            has_stdin_log = True
            stdin_log = open(self._cfg.tee_stdin_file, 'w')
        else:
            has_stdin_log = False
            
        # Start by sending our special `hostname` command:
        cmd = f'echo "{self._cfg.__class__.SHELL_OUTPUT_PREFIX}HOSTNAME=$(hostname)"\n'
        if has_stdin_log: stdin_log.write(cmd)
        dst.write(cmd)
        dst.flush()
        log_debug('Wrote startup command to remote shell: %s', cmd)
        
        # Allow subclasses to send additional commands before we start proxying:
        self.notify_stdin_ready(dst, stdin_log=stdin_log)

        # state for the loop below
        has_cli_listen_args = False

        for line in iter(src.readline, ''):
            m: 're.Match[str]|EllipsisType|None' = ...

            # Fix localhost in commands:
            if '--host=127.0.0.1' in line:
                m = self.__class__.RE_NODEJS_LOCALHOST.search(line)
                if m:
                    log_debug('NODEJS localhost line found: %s', line)
                    line = line.replace('127.0.0.1', '0.0.0.0')
            elif not has_cli_listen_args and '"$CLI_PATH" command-shell' in line:
                m = self.__class__.RE_CLI_CMD_LOCALHOST.search(line)
                if m:
                    log_debug('CLI_CMD localhost line found: %s', line)
                    line = self.__class__.RE_CLI_CMD_LOCALHOST.sub(
                        r'\g<1> --on-host=0.0.0.0 \g<2>', line
                    )
            elif 'LISTEN_ARGS=' in line:
                m = self.__class__.RE_CLI_LISTENARGS_LOCALHOST.search(line)
                if m:
                    log_debug('CLI_LISTENARGS localhost line found: %s', line)
                    line = self.__class__.RE_CLI_LISTENARGS_LOCALHOST.sub(
                        r'\g<1> --on-host=0.0.0.0 ', line
                    )
                    has_cli_listen_args = True
            
            if m is not ...:
                log_debug('localhost line found and fixed: %s', line)
    
            # Allow subclasses to possibly modify the line before passing it to the backend:
            line = self.notify_did_read_stdin(line)
            
            # Write the line to the stdin log and the backend's stdin:
            if has_stdin_log: stdin_log.write(line)
            dst.write(line)
            dst.flush()
        
        self.notify_did_close_stdin()
        
        # Close the stdin log file:
        if has_stdin_log: stdin_log.close()

        # All done, let everyone know:
        proxy.set_state(ProxyStates.END, msg='stdin thread')
    
    def notify_stdin_ready(self, backend_stdin: TextIO, stdin_log: 'TextIO|None'):
        """A callback that can be implemented by subclasses to execute code before commands from the client are proxied.  Can be used to send additional commands to the backend shell."""
        pass
    def notify_did_read_stdin(self, line: str) -> str:
        """A callback that can be implemented by subclasses to execute code after a line is read from the client stdin.  The line can be modified but must ultimately be returned to the caller."""
        return line
    def notify_did_close_stdin(self):
        """A callback that can be implemented by subclasses to execute code after the backend stdin has closed."""
        pass

    # Regex for the line output by the vscode backend indicating the TCP port on which it is listening:
    RE_TARGET_PORT = re.compile(
            r'^([^0-9]*listeningOn=[^0-9]*)(([0-9]+\.){3}[0-9]+:)?([0-9]+)([^0-9]*)$')

    def _stdout_proxy_fn(self, src: TextIO, dst: TextIO, proxy: VSCodeTCPProxy):
        """Consume output to the backend's stdout and write it to the client's stdout.  The lines must be scanned for output associated with commands issued by the _stdin_proxy_fn() thread.  In particular, the backend binary TCP port must be extracted from the output so that the binary TCP proxy can be told which port to connect with the client.

When EOF is reached on the backend's stdout the state is incremented to END, yielding the shutdown of this script -- the connection to the remote shell has been severed.
"""
        # Check the config for a stdout tee file:
        if self._cfg.tee_stdout_file:
            has_stdout_log = True
            stdout_log = open(self._cfg.tee_stdout_file, 'w')
        else:
            has_stdout_log = False
            
        listen_on_had_host = False
        backend_port_line: 'str | None' = None
        
        # Allow subclasses to send additional output before we start proxying:
        self.notify_stdout_ready(dst)

        for line in iter(src.readline, ''):
            omit = False  # forward the line to the destination

            # Check if it's an injected command
            if line.startswith(self._cfg.__class__.SHELL_OUTPUT_PREFIX):
                line = str_removeprefix(line, self._cfg.__class__.SHELL_OUTPUT_PREFIX)
                if line.startswith('HOSTNAME='):
                    proxy.backend_host = str_removeprefix(line, 'HOSTNAME=').strip()
                    log_info('Remote hostname found:  %s', proxy.backend_host)
                omit = True

            # Check if it contains the backend_port
            elif proxy.backend_port is None and "listeningOn=" in line:
                log_debug('Remote vscode TCP listener port found: %s', line)
                m = self.__class__.RE_TARGET_PORT.search(line)
                if m:
                    proxy.backend_port = int(m.group(4))
                    log_info('Remote TCP port found: %d', proxy.backend_port)
                    if m.group(2):
                        listen_on_had_host = m.group(2) != ''

                # Don't print the line now, stash it for output once the TCP proxy
                # has started:
                omit = True
                backend_port_line = line
            
            # Allow subclasses to see/alter the line if we didn't omit it:
            if not omit: line = self.notify_did_read_stdout(line)
            
            if (proxy.state == ProxyStates.BEGIN
                and proxy.backend_addr
                and backend_port_line is not None
            ):
                # Before going any further, start the proxy:
                proxy.set_state(ProxyStates.START_PROXY, msg='stdout thread')
                
                # Once it's started we can continue:
                proxy.wait_for(
                    ProxyStates.PROXY_STARTED,
                    msg='stdout waiting for TCP Proxy startup...')

                # Reformat the line with the local listening port:
                target_host_str = '127.0.0.1:' if listen_on_had_host else ''
                target_port_line = self.__class__.RE_TARGET_PORT.sub(
                        rf'\g<1>{target_host_str:s}{proxy.backend_port:d}\g<5>',
                        backend_port_line)
                log_debug("Remote vscode TCP listener line rewritten: %s", backend_port_line)
                if has_stdout_log: stdout_log.write(backend_port_line)
                sys.stdout.write(backend_port_line)

            if not omit:
                if has_stdout_log: stdout_log.write(line)
                sys.stdout.write(line)
            sys.stdout.flush()
        
        self.notify_did_close_stdout()
        
        # Close the stdout log:
        if has_stdout_log: stdout_log.close()

        # All done, let everyone know:
        proxy.set_state(ProxyStates.END, msg="stdout thread")
    
    def notify_stdout_ready(self, backend_stdin: TextIO):
        """A callback that can be implemented by subclasses to execute code before output from the backend is proxied.  Can be used to send additional output to the client."""
        pass
    def notify_did_read_stdout(self, line: str) -> str:
        """A callback that can be implemented by subclasses to execute code after a line is read from the backend stdout.  The line can be modified but must ultimately be returned to the caller."""
        return line
    def notify_did_close_stdout(self):
        """A callback that can be implemented by subclasses to execute code after the client stdout has closed."""
        pass
    
    def _stderr_proxy_fn(self, src: TextIO, dst: TextIO, proxy: VSCodeTCPProxy):
        """Consume output to the backend's stderr and write it to this script's stderr."""
        # Check the config for a stderr tee file:
        if self._cfg.tee_stderr_file:
            has_stderr_log = True
            stderr_log = open(self._cfg.tee_stderr_file, 'w')
        else:
            has_stderr_log = False
        
        # Allow subclasses to send additional output before we start proxying:
        self.notify_stderr_ready(dst)
            
        for line in iter(src.readline, ''):
            line = self.notify_did_read_stderr(line)
            if has_stderr_log: stderr_log.write(line)
            dst.write(line)
            dst.flush()
            
        self.notify_did_close_stderr()
        
        # Close the stderr log:
        if has_stderr_log: stderr_log.close()
    
    def notify_stderr_ready(self, backend_stdin: TextIO):
        """A callback that can be implemented by subclasses to execute code before output from the backend is proxied.  Can be used to send additional output to the client."""
        pass
    def notify_did_read_stderr(self, line: str) -> str:
        """A callback that can be implemented by subclasses to execute code after a line is read from the backend stderr.  The line can be modified but must ultimately be returned to the caller."""
        return line
    def notify_did_close_stderr(self):
        """A callback that can be implemented by subclasses to execute code after the client stderr has closed."""
        pass

# Set the default backend launcher class — a local proxy config script can alter this:
VSCodeBackendLauncherClass = VSCodeBackendLauncher
        


async def primary_runloop(cfg, proxyClass, backendClass):
    """The main asyncio event loop for the program.  Starts the binary TCP proxy and backend launcher, which will then synchronize between themselves to move the state forward.  We simply sit back and wait for the binary TCP proxy to transition to the END state, then cleanup the proxies and threads."""
    # Get the binary TCP proxy created and ready to go:
    tcp_proxy = proxyClass(cfg).start()
    
    # Get the backend launcher created and running:
    backend_launcher = backendClass(cfg).start(tcp_proxy)
    
    # Block the main thread until the binary TCP proxy ends:
    tcp_proxy.wait_for(ProxyStates.END, msg='Awaiting proxy termination')
    
    # Terminate the backend:
    backend_launcher.stop()
    
    # Terminate the binary TCP proxy:
    await tcp_proxy.stop()
    

#
# Check for a local config script adjacent to this script on the filesystem.
# If present, then execute that script in this context.
#
local_script_path = VSCodeProxyConfig.load_local_script(global_ns=globals(), local_ns=locals())


#
# Get started by generating the configuration for the proxy from defaults
# and CLI arguments:
#
cfg = VSCodeProxyConfigClass.create_with_cli_args().init_logging()
if local_script_path:
    log_info('Loaded and executed local site config from `%s''', local_script_path)
log_debug('Config class is "%s"', cfg.__class__.__name__)
log_debug('Config initialized as %s', cfg.__dict__)

#
# Run the proxy:
#
asyncio_run(primary_runloop(cfg, proxyClass=VSCodeTCPProxyClass, backendClass=VSCodeBackendLauncherClass))
