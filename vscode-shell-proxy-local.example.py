#
# Template for site configuration scripts
#

class VSCodeProxyConfigExample(VSCodeProxyConfig):
    """Configuration subclass template.  Delete methods that you do not need."""
    
    # Descriptive single-word name of your backend process launcher (e.g. 'salloc'):
    SCHEDULER_NAME: str = 'name of your backend process launcher'

    @classmethod
    def cli_parser(cls):
        """This method can be used to add argparse CLI options.  The superclass method is called to get the base argparse instance; arguments can be added; then the augmented instance is returned."""
        p = super().cli_parser()
        return p
    
    def set_defaults(self):
        """Supply default values for additional instance variables in the receiver.  The superclass method must be called before additional variables are set."""
        super().set_defaults()

# Tell the main script to use this subclass to handle the configuration:
VSCodeProxyConfigClass = VSCodeProxyConfigExample


class VSCodeTCPProxyExample(VSCodeTCPProxy):
    """Binary TCP proxy subclass template.  Delete methods that you do not need."""
    
    def notify_will_listen(self):
        """A callback that can be implemented by subclasses to execute code before the proxy begins listening for connections."""
        pass
    def notify_is_listening(self):
        """A callback that can be implemented by subclasses to execute code after the proxy is listening for connections.  The state has not transitioned to PROXY_STARTED yet."""
        pass

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

    def notify_did_read_io_stream_data(self, direction: ProxyDirection, data: bytes):
        """A callback that can be implemented by subclasses to execute code after a backend i/o stream has read data."""
        pass
    def notify_did_write_io_stream_data(self, direction: ProxyDirection, data: bytes):
        """A callback that can be implemented by subclasses to execute code after a backend i/o stream has written data."""
        pass
        
# Tell the main script to use this subclass to handle the binary TCP proxy:
VSCodeTCPProxyClass = VSCodeTCPProxyExample


class VSCodeBackendLauncherExample(VSCodeBackendLauncher):
    """Backend launcher subclass template.  Delete methods that you do not need."""

    def job_scheduler_base_cmd(self) -> List[str]:
        """All subclasses MUST implement this method to return the base command line -- up to user-provided arguments (via -S) -- that will be used to launch the backend."""
        return ['scheduler-launch-command']
    
    def get_job_scheduler_cmd(self) -> List[str]:
        """Implement this method to add arguments AFTER the base command and user-supplied arguments."""
        cmd = super().get_job_scheduler()
        cmd.append('another-option')
        return cmd
    
    def get_job_env(self) -> dict:
        """Implement this method to alter the environment for the backend launcher subprocess."""
        e = super().get_job_env()
        e['FUTURAMA_CHARACTER'] = 'HERMES'
        return e

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

    def notify_will_terminate_backend(self, job_proc: 'subprocess.Popen'):
        """A callback that can be implemented by subclasses to execute code immediately before the backend process is terminated."""
        pass
    def notify_did_terminate_backend(self, job_proc: 'subprocess.Popen'):
        """A callback that can be implemented by subclasses to execute code immediately before the backend process is terminated."""
        pass
        
    def notify_stdin_ready(self, backend_stdin: TextIO, stdin_log: 'TextIO|None'=None):
        """A callback that can be implemented by subclasses to execute code before commands from the client are proxied.  Can be used to send additional commands to the backend shell."""
        pass
    def notify_did_read_stdin(self, line: str) -> str:
        """A callback that can be implemented by subclasses to execute code after a line is read from the client stdin.  The line can be modified but must ultimately be returned to the caller."""
        return line
    def notify_did_close_stdin(self):
        """A callback that can be implemented by subclasses to execute code after the backend stdin has closed."""
        pass
        
    def notify_stdout_ready(self, backend_stdin: TextIO, stdout_log: 'TextIO|None'=None):
        """A callback that can be implemented by subclasses to execute code before output from the backend is proxied.  Can be used to send additional output to the client."""
        pass
    def notify_did_read_stdout(self, line: str) -> str:
        """A callback that can be implemented by subclasses to execute code after a line is read from the backend stdout.  The line can be modified but must ultimately be returned to the caller."""
        return line
    def notify_did_close_stdout(self):
        """A callback that can be implemented by subclasses to execute code after the client stdout has closed."""
        pass

    def notify_stderr_ready(self, backend_stdin: TextIO, stderr_log: 'TextIO|None'=None):
        """A callback that can be implemented by subclasses to execute code before output from the backend is proxied.  Can be used to send additional output to the client."""
        pass
    def notify_did_read_stderr(self, line: str) -> str:
        """A callback that can be implemented by subclasses to execute code after a line is read from the backend stderr.  The line can be modified but must ultimately be returned to the caller."""
        return line
    def notify_did_close_stderr(self):
        """A callback that can be implemented by subclasses to execute code after the client stderr has closed."""
        pass

# Tell the main script to use this subclass to handle the backend launch:
VSCodeBackendLauncherClass = VSCodeBackendLauncherExample
