#
# Site configuration script to replicate customizations
# of Andreas Poehlmann
#


class VSCodeProxyConfigPoehlmann(VSCodeProxyConfig):
    """Configuration subclass that adds functionality associated with PR-3."""
    
    # We use the Slurm job scheduler and the `srun` command to start interactive jobs:
    SCHEDULER_NAME: str = "srun"

VSCodeProxyConfigClass = VSCodeProxyConfigPoehlmann

#
# We don't need a customized binary TCP proxy class.
#

class VSCodeBackendLauncherPoehlmann(VSCodeBackendLauncher):
    """Backend launcher subclass that adds functionality associated with PR-3."""

    def job_scheduler_base_cmd(self) -> List[str]:
        import getpass
        user = (getpass.getuser() or "unknown").upper()
        return ["srun",
                "--ntasks=1",
                "--cpus-per-task=4",
                "--mem-per-cpu=4G",
                f"--job-name=vscode-proxy-{user}",
                "--qos=interactive",
                "--time=02:00:00",
                "--nodes=1"]
                
    def get_job_scheduler_cmd(self) -> List[str]:
        """Augments the baseline command with the shell command."""
        cmd = super().get_job_scheduler_cmd()
        cmd.append("/bin/bash")
        return cmd
    
    def get_job_env(self) -> dict:
        """Returns a dictionary containing the key-value pairs that should be present in the backend subprocess's environment."""
        import hashlib
        from uuid import uuid4
        from pathlib import Path
        
        short_uuid = hashlib.sha256(str(uuid4()).encode()).hexdigest()[:16]
        run_dir = Path.home().joinpath(".run", short_uuid)
        run_dir.mkdir(exist_ok=True, parents=True)
        
        env_dict = super().get_job_env()
        # Store the head node's ssh_auth_sock location
        env_dict["LOGIN_SSH_AUTH_SOCK"] = os.environ.get("SSH_AUTH_SOCK", "")
        # Set the runtime dir on the node to a writable location unique to the job
        env_dict["XDG_RUNTIME_DIR"] = str(run_dir.absolute())
        return env_dict
    
    def notify_stdin_ready(self, backend_stdin: TextIO, stdin_log: 'TextIO|None'):
        # Setup ssh-agent forwarding from the headnode to the node
        ssh_fwd_cmd = (
            "ssh -q -NfL ~/.ssh/$(hostname).sock:$LOGIN_SSH_AUTH_SOCK"
            ' -o "StreamLocalBindUnlink=yes" -o "ExitOnForwardFailure=yes"'
            " $SLURM_SUBMIT_HOST"
            " && export SSH_AUTH_SOCK=~/.ssh/$(hostname).sock"
            " && ssh-add -L > /dev/null 2>&1\n"
        )
        if stdin_log: stdin_log.write(ssh_fwd_cmd)
        backend_stdin.write(ssh_fwd_cmd)
        backend_stdin.flush()
        log_debug("Wrote SSH_AUTH_SOCK forwarding command to remote shell: %s", ssh_fwd_cmd)

VSCodeBackendLauncherClass = VSCodeBackendLauncherPoehlmann
