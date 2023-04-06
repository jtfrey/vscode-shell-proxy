# vscode-slurm-proxy

Microsoft's Visual Studio Code (vscode) application has a **Remote-SSH** extension that allows for remote execution of code.  This is intended to allow the developer to edit/run/debug in the (eventual) target environment or access hardware not present in the local host (e.g. GPUs).

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

Connecting to the "ClusterName" host in the vscode app worked as expected — initially.  The logged output shown in the vscode app indicated that the vscode remote software was downloaded and unpacked and a new session was created.  But then the app attempted to connect to a remote TCP port that existed on the compute node — by doing a second ssh connection with a TCP tunnel to that port *on the login node*.  Turning on "Enable Dynamic Forwarding" in the **Remote-SSH** extension configuration to facilitate dynamic tunnels over the open connection did not help because the remote sshd would still be trying to connect to the port on the login node.

The vscode remote session port answered to telnet and it was obvious that an HTTP-based service was behind it.  So beyond proxying the stdin/stdout/stderr to a shell in an interactive job, it would be necessary to proxy that TCP port.

## TCP proxy

In response, I split the `vscode-remote-shell` script into two worker scripts that would be spawned in the background by the original script.  One would include the original `salloc` remote shell instantiation, the other would use `ssh` to establish a tunnel to the remote TCP port on the login node.

Establishing the TCP port tunnel required that the output from `salloc` be scanned as it was passed back to the vscode app.  A line including the text

```
listeningOn==<port-number>==
```

would provide the necessary info.  The `salloc` command was piped to `tee` and stdout dumped to a uniquely-named log file.  The port tunnel script scanned that file in a loop until that line was found and the `ssh` tunnel would be created.

This turned out to not work:  at the same time that `listeningOn` text was being written to the local log file it was being returned to the vscode app, which *immediately* tried to connect.  Under that race condition the TCP port tunnel script was losing the race.  It seemed like the answer was to hold the `listeningOn` line and return it to the vscode app only after the TCP port tunnel was online.

## No more shell script

At this point the procedure had outgrown implementation as Bash shell scripts.  Python supports the standard `select()`-oriented serial multiplexing of i/o (among many other schemes) and can easily intercept and forward stdin/stdout/stderr.  The new `vscode-remote-shell.py` script was far more complex and even had CLI flags that the vscode app could make use of:

```
usage: vscode-remote-shell.py [-h] [-v] [-q] [-l <PATH>] [--tee-stdin <PATH>]
                              [--tee-stdout <PATH>] [--tee-stderr <PATH>]
                              [-b <N>] [-B <N>] [-p <N>] [-g <WORKGROUP>]
                              [-S <SLURM-ARG>]

vscode cluster proxy

optional arguments:
  -h, --help            show this help message and exit
  -v, --verbose         increase the level of output as the program executes
  -q, --quiet           decrease the level of output as the program executes
  -l <PATH>, --log-file <PATH>
                        direct all logging to this file rather than stderr
  --tee-stdin <PATH>    send a copy of input to the script stdin to this file
  --tee-stdout <PATH>   send a copy of output to the script stdout to this
                        file
  --tee-stderr <PATH>   send a copy of output to the script stderr to this
                        file
  -b <N>, --backlog <N>
                        number of backlogged connections held by the proxy
                        socket (see man page for listen(), default 8)
  -B <N>, --byte-limit <N>
                        maximum bytes read at one time per socket (default
                        4096
  -p <N>, --listen-port <N>
                        the client-facing TCP proxy port (default 0 implies a
                        random port is chosen)
  -g <WORKGROUP>, --group <WORKGROUP>, --workgroup <WORKGROUP>
                        the workgroup used to submit the vscode job
  -S <SLURM-ARG>, --salloc-arg <SLURM-ARG>
                        used zero or more times to specify arguments to the
                        salloc command being wrapped (e.g. --partition=<name>,
                        --ntasks=<N>)
```

For the sake of debugging, the `--tee-*` and `--log-file` flags were of critical importance.  The `--group` and `--salloc-arg` flags removed the hard-coded values present in the original scripts.

The script worked:  intercepting the `listeningOn` line and holding it while the listener socket for the TCP proxying was added yielded (with "Enable Dynamic Forwarding" on) the originating connection's being able to connect to the TCP port!  But this did not equate with success.

## The mechanism revealed

The `vscode-remote-shell.py` proxy worked as expected — so why wasn't the vscode app happy?  It turns out this is tied to the **Remote-SSH** remote code's reconnect capability.  Their scripts are executed with no controlling terminal, so when the connection drops they receive no SIGHUP and keep running.  When the vscode app reconnects to the remote system it cannot hook into the stdin/stdout/stderr of those programs:  that's why the TCP port is there.

Anecdotal evidence shows that after establishing the new remote session on the remote host, the vscode app purposefully closes the connection.  It then attempts a reconnect via the TCP port so that all **Remote-SSH** infrastructure post-setup happens via the HTTP-like protocol.  Unfortunately when it drops that originating connection, the `salloc` interactive job is terminated and the session goes away.

In the end, by construction the **Remote-SSH** plugin doesn't lend itself to this kind of automation and integration with HPC systems.
