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
from enum import Enum

#
# The following are used for type-checking throughout the code; some of the
# types are handled differently across 3.x versions.
#
from typing import TYPE_CHECKING, Any, TextIO
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
                help='the client-facing TCP proxy should bind to this interface (default {:s}; use 0.0.0.0 for all interfaces)'.format(cls.DEFAULT_LISTEN_HOST))
        cli_parser.add_argument('-p', '--listen-port', metavar='<N>',
                dest='listen_port',
                default=cls.DEFAULT_LISTEN_PORT,
                type=int,
                help='the client-facing TCP proxy port (default {:d})'.format(cls.DEFAULT_LISTEN_PORT))
        cli_parser.add_argument('-S', '--salloc-arg', metavar='<SLURM-ARG>',
                dest='salloc_args',
                action='append',
                help='used zero or more times to specify arguments to the salloc command being wrapped (e.g. --partition=<name>, --ntasks=<N>)')
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
        print(self.verbosity)
        # Allow CLI arguments to override defaults:
        super(VSCodeProxyConfig, self).__init__(**kwargs)
        print(self.verbosity)
    
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
    def target_addr(self) -> 'Tuple[str, int] | None':
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




#
# Check for a local config script adjacent to this script on the filesystem.
# If present, then execute that script in this context.
#
local_cfg_script = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'vscode-shell-proxy-local.py')
if os.path.isfile(local_cfg_script):
    try:
        with open(local_cfg_script, 'r') as fptr:
            local_cfg_src = fptr.read()
    except Exception as err:
        sys.stderr.write('ERROR:  unable to open local proxy config script for reading: {:s}\n'.format(str(err)))
        sys.exit(-1)
    try:
        local_cfg_code = compile(local_cfg_src, local_cfg_script, 'exec')
        exec(local_cfg_code)
    except Exception as err:
        import traceback
        sys.stderr.write('ERROR:  unable to execute local proxy config script:  {:s}\n'.format(str(err)))
        traceback.print_tb(err.__traceback__, file=sys.stderr)
        sys.exit(-1)
else:
    local_cfg_script = ''

#
# Get started by generating the configuration for the proxy from defaults
# and CLI arguments:
#
cfg = VSCodeProxyConfigClass.create_with_cli_args().init_logging()
if local_cfg_script:
    log_info('Local configuration script executed from file "%s"', local_cfg_script)
log_debug('Config class is "%s"', cfg.__class__.__name__)
log_debug('Config initialized as %s', cfg.__dict__)





tcp_proxy = VSCodeTCPProxyClass(cfg).start()
tcp_proxy.backend_host = '127.0.0.1'
tcp_proxy.backend_port = 22
print(tcp_proxy)
tcp_proxy.set_state(ProxyStates.START_PROXY, msg='Backend started')

from time import sleep

sleep(3000)