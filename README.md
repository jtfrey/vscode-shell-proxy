# vscode-shell-proxy

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

  - Set "Config File" to something other than your default ssh file (you don't want regular `ssh` using these settings).
  - Raise "Connect Timeout" to e.g. 300 seconds since the job scheduler may take some time to get the remote shell running.
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

At this point the procedure had outgrown implementation as Bash shell scripts.  Python asyncio and threading can easily multiplex the forwarding of TCP ports and stdin/stdout/stderr.  The new `vscode-shell-proxy.py` script was far more complex and even had CLI flags that the vscode app could make use of:

```
usage: vscode-shell-proxy.py [-h] [-v] [-q] [-l <PATH>] [-0 <PATH>] [-1 <PATH>] [-2 <PATH>] [-b <N>] [-B <N>]
                             [-H <HOSTNAME>] [-p <N>] [-S <SALLOC-ARG>] [-g <WORKGROUP>]

vscode remote shell proxy

optional arguments:
  -h, --help            show this help message and exit
  -v, --verbose         increase the level of output as the program executes
  -q, --quiet           decrease the level of output as the program executes
  -l <PATH>, --log-file <PATH>
                        direct all logging to this file rather than stderr; the token "[PID]" will be replaced with
                        the running pid
  -0 <PATH>, --tee-stdin <PATH>
                        send a copy of input to the script stdin to this file; the token "[PID]" will be replaced with
                        the running pid
  -1 <PATH>, --tee-stdout <PATH>
                        send a copy of output to the script stdout to this file; the token "[PID]" will be replaced
                        with the running pid
  -2 <PATH>, --tee-stderr <PATH>
                        send a copy of output to the script stderr to this file; the token "[PID]" will be replaced
                        with the running pid
  -b <N>, --backlog <N>
                        number of backlogged connections held by the proxy socket (see man page for listen(), default
                        8)
  -B <N>, --byte-limit <N>
                        maximum bytes read at one time per socket (default 4096)
  -H <HOSTNAME>, --listen-host <HOSTNAME>
                        the client-facing TCP proxy should bind to this interface (default 127.0.0.1; 0.0.0.0 => all
                        interfaces)
  -p <N>, --listen-port <N>
                        the client-facing TCP proxy port (default 0; 0 => random port)
  -S <SALLOC-ARG>, --salloc-arg <SALLOC-ARG>
                        used zero or more times to specify arguments to the salloc command that launches the backend
  -g <WORKGROUP>, --group <WORKGROUP>, --workgroup <WORKGROUP>
                        the workgroup used to submit the vscode job
```

For the sake of debugging, the `--tee-*` and `--log-file` flags were of critical importance.  The `--group` and `--salloc-arg` flags removed the hard-coded values present in the original scripts.  See the [version 2.0.0](#v2.0.0) section in this document for additional information about the site-specific flags and behaviors of the script.

The script worked:  intercepting the `listeningOn` line and holding it while the listener socket for the TCP proxying was added yielded (with "Enable Dynamic Forwarding" on) the originating connection's being able to connect to the TCP port on the login node.

But the proxy script could not connect to the TCP port on the compute node that the vscode backend had reported.  After an embarrasing number of false starts to debug the problem, a `ps` on the compute node revealed the problem:  the TCP port was bound to 127.0.0.1.  The **Remote-SSH** extension sends the commands to start the backend scripts with `--host=127.0.0.1` among the flags.  The scripts in question will happily accept `--host=0.0.0.0` instead and bind to all TCP interfaces on the compute node.

Binding to `localhost` implies a TCP port is only reachable on the machine itself.  But beyond that, anyone with an account on the machine can connect to that TCP port so there's no security beyond isolation from the Internet.  Since most computers these days run local firewalling software that accomplishes the same isolation — our clusters' login nodes included — the software gains no additional security binding to `localhost` versus every interface (`0.0.0.0`).  Since the proxy script is already receiving and forwarding stdin from the VSCode application, it was very easy to introduce code to catch the commands containing the offending `--host=127.0.0.1` and alter it to `--host=0.0.0.0` before forwarding the command to the remote shell.

With that change, the proxy worked perfectly.  Additional testing with user-forwarded ports in the VSCode application demonstrated that under this setup, there was no additional code necessary for that feature.

### Changes for the Binary Exec Server

The situation changed in early 2024 when Microsoft began deploying a binary remote agent ("exec server") in lieu of the node.js agent previously used.  The exec server defaults to binding to 127.0.0.1 and no equivalent to the traditional variant's `--host` flag was initially made available.  An [issue was filed](https://github.com/microsoft/vscode-remote-release/issues/9713) and Microsoft did eventually add an `--on-host` flag to the exec server, which allowed vscode-shell-proxy to be altered to fixup the startup command to bind to 0.0.0.0 instead.  As of 2024-04-10 this functionality was only available in the VSCode Insiders release stream, but eventually it should make it into the mainstream releases, too.

## Production setup

The script requires Python 3, so a dedicated build of Python 3 was made from source and installed in `<install-prefix>/python-root`.  The proxy script was modified so that its hash-bang used `<install-prefix>/python-root/bin/python3` as its interpreter and the script was installed as `<install-prefix>/bin/vscode-shell-proxy.py`.

The final settings used for the **Remote-SSH** extension were:

  - Set "Config File" to something other than your default ssh file (you don't want regular `ssh` using these settings).
  - Raise "Connect Timeout" to e.g. 300 seconds since the job scheduler may take some time to get the remote shell running.
  - *enable* "Enable Remote Command"
  - *enable* "Use Local Server"
  - *enable* "Enable Dynamic Forwarding"

Two hosts were added to the "Config File":

```
# Read more about SSH config files: https://linux.die.net/man/5/ssh_config
Host Caviness
    HostName caviness.hpc.udel.edu
    User frey
    RemoteCommand <install-prefix>/bin/vscode-shell-proxy.py -g it_nss --salloc-arg=--partition=devel --salloc-arg=--cpus-per-task=4

Host Caviness-verbose
    HostName caviness.hpc.udel.edu
    User frey
    RemoteCommand <install-prefix>/bin/vscode-shell-proxy.py -vvvv -g it_nss --salloc-arg=--partition=devel --salloc-arg=--cpus-per-task=4 -l /home/1001/.vscode-remote-shell.log.[PID] --tee-stdin=/home/1001/.vscode-stdin.log.[PID] --tee-stdout=/home/1001/.vscode-stdout.log.[PID] --tee-stderr=/home/1001/.vscode-stderr.log.[PID]
```

Both configurations used the **devel** partition on Caviness (which has a 2 hour wall time limit) and request 4 CPUs for the remote shell to use.  The latter configuration was used to debug issues while connecting with the VSCode application — the extensive logging is not recommended for normal use of this facility.

### Simpler invocation

Having such a lengthy path in the RemoteCommand to use `vscode-shell-proxy.py` may produce some user error.  For the sake of simplifying its usage, a symbolic link `vscode-shell-proxy` was added to `/usr/local/bin` on the login nodes.  An example host configuration becomes:

```
Host Caviness
    HostName caviness.hpc.udel.edu
    User frey
    RemoteCommand vscode-shell-proxy -g it_nss --salloc-arg=--partition=devel --salloc-arg=--cpus-per-task=4
```

### X11 forwarding

The **Remote-SSH** extension includes an option that will honor X11 forwarding configuration options present in the ssh host configuration file:

```
Host Caviness+X11
    HostName caviness.hpc.udel.edu
    User frey
    ForwardX11 yes
    ForwardX11Trusted yes
    RemoteCommand vscode-shell-proxy -g it_nss --salloc-arg=--partition=devel --salloc-arg=--cpus-per-task=4 --salloc-arg=--x11
```

In the **Remote-SSH** extension configuration:

  - *enable* "Enable X11 Forwarding"

With a local X11 server running next to the VSCode application, the remote side of the ssh connection will have `DISPLAY` configured in its environment.  When the `salloc` command is executed in that remote environment, the `--x11` flag can be added (using `--salloc-arg=--x11` in the RemoteCommand) and Slurm itself will handle forwarding of X11 traffic between the compute node and login node — where the ssh connection will forward between the login node and your computer.

## Periodic Cleanup

Anyone supporting users on HPC resources knows that documenting the appropriate way to do something on the system doesn't mean all users will find that information, read it, and employ it in their workflows.  With **Remote-SSH**, users who discover the tool will inevitably follow Microsoft's instructions and wind up leaving behind orphaned sessions on the login nodes, which will be noticed and prompt IT staff's contacting them to correct their behavior.

The [vscode-cleanup.py](./vscode-cleanup.py) script attempts to help the situation by identifying vscode process trees whose root process is orphaned (ppid 1) and killing that process and its descendents.

## <a name="v2.0.0"></a>Site-specific Modifications

The original release of this script embedded many system-specific behaviors.  UD's clusters use the Slurm job scheduler and `salloc` is configured with acceptable default flags, so the backend shell was started quite simply with `salloc`.  The `salloc` must be executed with a specific workgroup gid selected, so the command is wrapped by our `workgroup` command.  None of this is likely usable by other sites, and that was proved in the code provided on PR #3.

In version 2.0.0, the class-based refactoring of the script from PR #3 was used.  The runtime configuration was also encapsulated in a class.  A number of event-based notification methods were introduced into the classes, allowing subclasses to hook into the binary TCP proxy and backend launcher runloops, e.g. for the sake of sending additional commands to the backend shell or parsing stdout for additional information.

The intention is for subclassing to be used to extend the functionality of the baseline script.  Sites need not modify `vscode-shell-proxy.py`; rather, in the same directory as that script a `vscode-shell-proxy-local.py` source file is loaded, compiled, and executed at runtime.  The code in that local config script should include subclasses that implement the site-specific behaviors.  See the [vscode-shell-proxy-local.example.py](./vscode-shell-proxy-local.example.py) script for template subclasses.  The site-specific modifications necessary for UD's clusters and for the PR #3 contributor's system are encapsulated in the [vscode-shell-proxy-local.UDelDarwin.py](./vscode-shell-proxy-local.UDelDarwin.py) and [vscode-shell-proxy-local.Poehlmann.py](./vscode-shell-proxy-local.Poehlmann.py) scripts, respectively.

For example, we can install the **vscode-shell-proxy** in a versioned hierarchy:

```bash
$ mkdir /opt/shared/vscode-shell-proxy
$ cd /opt/shared/vscode-shell-proxy
$ git clone https://github.com/jtfrey/vscode-shell-proxy.git src
$ cd src
$ git checkout v2.0.0
$ mkdir ../2.0.0
$ cp vscode-shell-proxy.py ../2.0.0
$ cp vscode-shell-proxy-local-example.py ../2.0.0/vscode-shell-proxy-local.py
```

Assuming we edit the `/opt/shared/vscode-shell-proxy/2.0.0/vscode-shell-proxy-local.py` file, we can install a symlink in `/usr/local/bin`:

```bash
$ sudo ln -s /opt/shared/vscode-shell-proxy/2.0.0/vscode-shell-proxy.py /usr/local/bin/vscode-shell-proxy
```

So long as `/usr/local/bin` is on the user's PATH, the `vscode-shell-proxy` command is available.  The script will first check for a local config adjacent to the symlink (`/usr/local/bin/vscode-shell-proxy-local.py`) and if that is not present, it will resolve the symlink and check adjacent to the target (`/opt/shared/vscode-shell-proxy/2.0.0/vscode-shell-proxy-local.py`).