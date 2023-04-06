# vscode-slurm-proxy

Microsoft's Visual Studio Code (vscode) application has an extension that allows for remote execution of code.  This is intended to allow the developer to edit/run/debug in the (eventual) target environment or access hardware not present in the local host (e.g. GPUs).

There are several issues with this when the target environment is an HPC cluster:

  - The remote infrastructure (shell and Python scripts) is left running indefinitely.
  - Users who become reliant on vscode as a production environment will inevitably have their work killed (since it will be executing outside of job scheduling).
  - Any specialized hardware (like GPUs, large RAM) is not present in login nodes.

The clusters currently in production at UD have multiple login nodes balanced via round-robin DNS resolution of a single hostname.  With regard to the first point above, the local vcsode application will "remember" the session it previously created and attempt to reconnect to it.  But since repeated DNS resolution of the hostname may not produce the same IP address, that session may not always be found — and a new one will be created while the old persists on a different login node.  We often see users accumulate many of these orphaned vscode remote sessions on our login nodes.

An attempt at proxying vscode remote sessions through the cluster job scheduler seemed like a worthy project.  If the vscode app's shell commands were forwarded to an interactive job running on a compute node, then the remote execution infrastructure would:

  - have access to specialized hardware by virtue of the parameters associated with the interactive job.
  - not consume significant CPU resources on the login node.
  - be automatically terminated when the interactive job completes.

## Shell proxy

It was obvious from watching how the remote environment setup was effected that the extension is sending commands to the remote shell.  So a starting point was to write a shell script that would `exec()` the Slurm `salloc` command with appropriate arguments and have the extension execute that command rather than settling for the default login shell.

Three extension settings had to be altered from their default:

  - Reset "Config File" to something other than your default ssh file (you don't want regular `ssh` using these settings).
  - Raise "Connect Timeout" to ca. 300 seconds since the job scheduler may take some time to get the remote shell running.
  - *enable* "Enable Remote Command"
  - *enable* "Use Local Server"

The remote command itself is specified when adding the host record to that "Config File":

```
# Read more about SSH config files: https://linux.die.net/man/5/ssh_config
Host ClusterName
    HostName login-node.cluster.name
    User myusername
    RemoteCommand <full-path-to>/vscode-remote-shell.sh
```

That incarnation of the `vscode-remote-shell` script was very simple:

```bash
#!/bin/bash -l

exec workgroup -g it_nss --command @ -- salloc -p devel

```

Connecting to the "ClusterName" host in the vscode app worked.  The logged output shown in the vscode app indicated that the vscode remote software was downloaded and unpacked and a new session was created.  But then the app attempted to connect to a remote TCP port that existed on the compute node — by doing a second ssh connection to a login node.
