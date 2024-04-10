#!/opt/shared/slurm/add-ons/vscode-shell-proxy/python-root/bin/python3
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

import asyncio
import time
import logging
import threading
import argparse
import subprocess
import re
import sys
import os
from enum import Enum

# This is the local TCP port on which this script is listening:
proxyPort = None

# This is the remote (compute node) hostname and TCP port on which vscode backend
# is listening:
targetHost = None
targetPort = None

# Regex for the line output by the vscode backend indicating the TCP port on which
# it is listening:
targetPortRegex = re.compile(r'^([^0-9]*listeningOn=[^0-9]*)(([0-9][0-9]*\.){3}[0-9][0-9]*:)?([0-9][0-9]*)([^0-9]*)$')

# Regex for input lines containing references to running servers bound to the
# localhost interface only:
localhostFixupNodeJSRegex = re.compile(r'((\$args =.*)|(\$VSCH_SERVER_SCRIPT.*))--host=127.0.0.1')
localhostFixupCLIRegex = re.compile(r'(VSCODE_CLI_REQUIRE_TOKEN=[0-9a-fA-F-]*.*\$CLI_PATH.*command-shell )(.*)(--on-host=(([0-9][0-9]*\.){3}[0-9][0-9]*))?')

# Any commands this script itself sends to the remote shell should have their output
# prefixed with this text to indicate they are NOT in response to VSCode application
# commands:
ourShellOutputPrefix = 'VSCODE_SHELL_PROXY::::'

# Default buffer size for binary TCP proxy i/o:
DEFAULT_BYTE_LIMIT = 4096

# Default connection acceptance backlog count:
DEFAULT_BACKLOG = 8

# The script progresses through these states:
class ProxyStates(Enum):
    LAUNCH = 0
    BEGIN = 1
    START_PROXY = 2
    PROXY_STARTED = 3
    END = 4

# This condition will be used to synchronize the progression of the script through
# operational states:
proxyStateCond = threading.Condition()
proxyState = ProxyStates.LAUNCH
def checkProxyState(desiredState):
    global proxyState
    return proxyState == desiredState


async def tcp_proxy_xfer(R, W):
    """Receive data from a reader and send it on a writer, closing and exiting from this function on any errors, end-of-file, etc."""
    global cliArgs
    
    while not R.at_eof() and not W.is_closing():
        binaryData = await R.read(cliArgs.byteLimit)
        if binaryData:
            # Send the data on the writer:
            W.write(binaryData)
            await W.drain()
        else:
            # No data implies the reader has closed:
            W.close()
            break


async def tcp_proxy_connect(sR, sW):
    """Accept a connection opened on the proxy port.  Open a connection to the vscode backend then create two async transfer functions to forward data between them."""
    sWAddr = sW.get_extra_info('peername')
    logging.info('{:s}:{:d} connection accepted'.format(*sWAddr))
    try:
        rR, rW = await asyncio.open_connection(targetHost, targetPort)
        async with asyncio.TaskGroup() as tg:
            t1 = tg.create_task(tcp_proxy_xfer(sR, rW))
            t2 = tg.create_task(tcp_proxy_xfer(rR, sW))
            logging.debug('{:s}:{:d} i/o tasks scheduled'.format(*sWAddr))
        logging.debug('{:s}:{:d} i/o tasks completed'.format(*sWAddr))
    except Exception as E:
        logging.error('{:s}:{:d} failure: {:s}'.format(sWAddr[0], sWAddr[1], str(E)))
        sW.close()


async def tcp_proxy():
    global proxyPort, proxyState, proxyStateCond, cliArgs
    """The vscode backend TCP port proxy server."""
    with proxyStateCond:
        logging.debug('    Waiting on TCP proxy server start condition...')
        proxyStateCond.wait_for(lambda:checkProxyState(ProxyStates.START_PROXY))
        logging.debug('    Starting TCP proxy server on port %d', cliArgs.listenPort)
        server = await asyncio.start_server(tcp_proxy_connect, cliArgs.listenHost, cliArgs.listenPort, reuse_port=True, backlog=cliArgs.backlog)
        # Get the port we're listening on:
        proxyPort = server.sockets[0].getsockname()[1]
        
        logging.info('    Running TCP proxy server on port %d...', proxyPort)
        proxyState = ProxyStates.PROXY_STARTED
        logging.debug('[STATE] PROXY_STARTED <- TCP proxy runloop')
        proxyStateCond.notify_all()
    async with server:
        await server.serve_forever()
    logging.debug('    Terminating TCP proxy server.')


def start_tcp_proxy(loop):
    """Alternative thread that will run the vscode backend TCP port proxy runloop."""
    asyncio.set_event_loop(loop)
    logging.debug('  Entering tcp proxy event loop')
    loop.run_until_complete(tcp_proxy())
    logging.debug('  Exited tcp proxy event loop')


def stdinProxyThread(drain, copyToFile=None):
    """Target function for a thread that will consume input from this script's stdin and write it to the remote shell's stdin.  Before any forwarding begins, introspective command(s) associated with this script are sent (and their output will be consumed by the stdout-forwarding thread).  When EOF is reached on this script's stdin the state is forwarded to END, yielding the shutdown of this script -- the connection from the VSCode application has been severed."""
    global proxyStateCond, proxyState, localhostFixupNodeJSRegex, localhostFixupCLIRegex
    
    # Start by sending our special `hostname` command:
    hostnameCmd = 'echo "{:s}HOSTNAME=$(hostname)"\n'.format(ourShellOutputPrefix)
    if copyToFile:
        copyToFile.write(hostnameCmd); copyToFile.flush()
    drain.write(hostnameCmd); drain.flush()
    logging.debug('Wrote startup command to remote shell: %s', hostnameCmd.strip())
    
    while True:
        logging.debug('Waiting on stdin...')
        inputLine = sys.stdin.readline()
        if not inputLine:
            break
        
        # Localhost fixups?
        if '--host=127.0.0.1' in inputLine:
            # Confirm it's one of the lines we're expecting:
            localhostFixupMatch = localhostFixupNodeJSRegex.search(inputLine)
            if localhostFixupMatch is None:
                logging.warning('unanticipated localhost line found: %s', inputLine.strip())
            else:
                logging.debug('localhost line found and fixed: %s', inputLine.strip())
                inputLine = inputLine.replace('127.0.0.1', '0.0.0.0')
        elif '"$CLI_PATH" command-shell' in inputLine:
            # Confirm it's one of the lines we're expecting:
            localhostFixupMatch = localhostFixupCLIRegex.search(inputLine)
            if localhostFixupMatch is None:
                logging.warning('unanticipated localhost line found: %s', inputLine.strip())
            else:
                logging.debug('localhost line found and fixed: %s', inputLine.strip())
                inputLine = re.sub(localhostFixupCLIRegex, r'\g<1> --on-host=0.0.0.0 \g<2>', inputLine)
        
        if copyToFile is not None:
            copyToFile.write(inputLine); copyToFile.flush()
        drain.write(inputLine); drain.flush()

    # All done, let everyone know:
    with proxyStateCond:
        proxyState = ProxyStates.END
        logging.debug('[STATE] END <- stdin thread')
        proxyStateCond.notify_all()


def stderrProxyThread(faucet, copyToFile=None):
    """Consume output to the remote shell's stderr and write it to this script's stderr."""
    if copyToFile is not None:
        while True:
            logging.debug('Waiting on remote stderr...')
            inputLine = faucet.readline()
            if not inputLine:
                break
            copyToFile.write(inputLine); copyToFile.flush()
            sys.stderr.write(inputLine); sys.stderr.flush()
    else:
        while True:
            inputLine = faucet.readline()
            if not inputLine:
                break
            sys.stderr.write(inputLine); sys.stderr.flush()


def stdoutProxyThread(faucet, copyToFile=None):
    """Consume output to the remote shell's stdout and write it to this script's stdout.  This function is far more complex compared to the stderrProxyThread() function:  the stdout lines must be scanned for output associated with commands issued by this script (e.g. to get the remote hostname) and the remote TCP port on which the vscode backend is listening.  Once those data are known, this script's TCP proxy can be started.  When EOF is reached on the remote shell's stdout the state is forwarded to END, yielding the shutdown of this script -- the connection to the remote shell has been severed."""
    global targetHost, targetPort, targetPortRegex, proxyStateCond, proxyState
    
    listenOnHadHost = False
    
    while True:
        logging.debug('Waiting on remote stdout...')
        outputLine = faucet.readline()
        if not outputLine:
            break
        
        omitLine = False
        
        # Is it one of our command(s)?
        if outputLine.startswith(ourShellOutputPrefix):
            # Drop the prefix:
            outputLine = outputLine[len(ourShellOutputPrefix):]
            
            # Is it the hostname line?
            if outputLine.startswith('HOSTNAME='):
                targetHost = outputLine[len('HOSTNAME='):].strip()
                logging.info('Remote hostname found:  %s', targetHost)
            
            # Never send these lines to the app:
            omitLine = True
        else:
            # Output coming back from the remote vscode stuff:
            if targetPort is None and 'listeningOn=' in outputLine:
                logging.debug('Remote vscode TCP listener port found: %s', outputLine.strip())
                targetPortMatch = targetPortRegex.search(outputLine)
                if targetPortMatch is not None:
                    targetPort = int(targetPortMatch.group(4))
                    listenOnHadHost = (len(targetPortMatch.group(2)) > 0)
                    logging.info('Remote TCP port found:  %d', targetPort)
                
                # Don't print the line now, stash it for output once the TCP proxy
                # has started:
                omitLine = True
                targetPortLine = outputLine

        if proxyState is ProxyStates.BEGIN and targetHost is not None and targetPort is not None:        
            # Before going any further, start the proxy:
            with proxyStateCond:
                proxyState = ProxyStates.START_PROXY
                logging.debug('[STATE] START_PROXY <- stdout thread')
                proxyStateCond.notify_all()

            # Once it's started we can continue:
            with proxyStateCond:
                logging.debug('stdout thread waiting for TCP proxy startup completed...')
                proxyStateCond.wait_for(lambda:checkProxyState(ProxyStates.PROXY_STARTED))
    
            # Reformat the line with the local listening port:
            targetHostStr = '127.0.0.1:' if listenOnHadHost else ''
            targetPortLine = re.sub(targetPortRegex, r'\g<1>{:s}{:d}\g<5>'.format(targetHostStr, proxyPort), targetPortLine)
            logging.debug('Remote vscode TCP listener line rewritten: %s', targetPortLine.strip())
        
            if copyToFile:
                copyToFile.write(targetPortLine); copyToFile.flush()
            sys.stdout.write(targetPortLine)

        if not omitLine:
            if copyToFile:
                copyToFile.write(outputLine); copyToFile.flush()
            sys.stdout.write(outputLine)
        
        sys.stdout.flush()

    # All done, let everyone know:
    with proxyStateCond:
        proxyState = ProxyStates.END
        logging.debug('[STATE] END <- stdout thread')
        proxyStateCond.notify_all()


async def runloop():
    """The main asyncio event loop for this script.  Starts the TCP port proxy so it will be awaiting a startup signal (once the remote hostname and TCP port are known).  Launches the remote shell with Slurm `salloc` and connects its stdio channels to threaded i/o handlers.  The function then goes to sleep until the program state reaches END, then cleans-up the remote shell subprocess and TCP proxy runloop before exiting."""
    global proxyState, proxyStateCond, cliArgs, teeFiles
    
    logging.debug('Runloop start')
    with proxyStateCond:
        proxyState = ProxyStates.BEGIN
        logging.debug('[STATE] BEGIN <- main runloop')
        proxyStateCond.notify_all()
    
    # Get a separate thread setup for the TCP proxy:
    proxyLoop = asyncio.new_event_loop()
    proxyThread = threading.Thread(name='TCP-Proxy', target=start_tcp_proxy, args=(proxyLoop,), daemon=True)
    proxyThread.start()
    
    # Start the remote shell:
    remoteShellCmd = ['workgroup', '-g', cliArgs.workgroup, '--command', '@', '--', 'salloc' ]
    if cliArgs.sallocArgs:
        remoteShellCmd.extend(cliArgs.sallocArgs)
    logging.debug('Command to launch remote shell: "%s"', ' '.join(remoteShellCmd))
    remoteShellProc = subprocess.Popen(
                                remoteShellCmd,
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True,
                                bufsize=1
                            )
    logging.info('Remote shell launched with pid %d', remoteShellProc.pid)


    # Start the stdio threads:
    stdinProxy = threading.Thread(
                        name='Remote-Shell-Stdin',
                        target=stdinProxyThread,
                        args=(remoteShellProc.stdin, teeFiles['stdin']),
                        daemon=True)
    stderrProxy = threading.Thread(
                        name='Remote-Shell-Stderr',
                        target=stderrProxyThread,
                        args=(remoteShellProc.stderr, teeFiles['stderr']),
                        daemon=True)
    stdoutProxy = threading.Thread(
                        name='Remote-Shell-Stdout',
                        target=stdoutProxyThread,
                        args=(remoteShellProc.stdout, teeFiles['stdout']),
                        daemon=True)
    stdoutProxy.start()
    stderrProxy.start()
    stdinProxy.start()
    with proxyStateCond:
        logging.debug('Awaiting proxy termination...')
        proxyStateCond.wait_for(lambda:checkProxyState(ProxyStates.END))
    
    # Terminate the remote shell:
    logging.info('Terminating remote shell process...')
    remoteShellProc.terminate()
    try:
        remoteShellProc.wait(timeout=10)
    except:
        remoteShellProc.kill()
    
    # Terminate the TCP proxy event loop:
    logging.debug('Terminating TCP proxy event loop...')
    await proxyLoop.shutdown_asyncgens()
    proxyLoop.stop()
    
    # We don't bother joining the i/o threads, they're daemons anyway.
    
    logging.debug('Proxy has terminated.')




loggingLevels = [ logging.CRITICAL, logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG ]
baseLoggingLevel = 1
        
cliParser = argparse.ArgumentParser(description='vscode remote shell proxy')
cliParser.add_argument('-v', '--verbose',
        dest='verbosity',
        default=0,
        action='count',
        help='increase the level of output as the program executes')
cliParser.add_argument('-q', '--quiet',
        dest='quietness',
        default=0,
        action='count',
        help='decrease the level of output as the program executes')
cliParser.add_argument('-l', '--log-file', metavar='<PATH>',
        dest='logFile',
        default=None,
        help='direct all logging to this file rather than stderr; the token "[PID]" will be replaced with the running pid')
cliParser.add_argument('-0', '--tee-stdin', metavar='<PATH>',
        dest='teeStdinFile',
        default=None,
        help='send a copy of input to the script stdin to this file; the token "[PID]" will be replaced with the running pid')
cliParser.add_argument('-1', '--tee-stdout', metavar='<PATH>',
        dest='teeStdoutFile',
        default=None,
        help='send a copy of output to the script stdout to this file; the token "[PID]" will be replaced with the running pid')
cliParser.add_argument('-2', '--tee-stderr', metavar='<PATH>',
        dest='teeStderrFile',
        default=None,
        help='send a copy of output to the script stderr to this file; the token "[PID]" will be replaced with the running pid')
cliParser.add_argument('-b', '--backlog', metavar='<N>',
        dest='backlog',
        default=DEFAULT_BACKLOG,
        type=int,
        help='number of backlogged connections held by the proxy socket (see man page for listen(), default {:d})'.format(DEFAULT_BACKLOG))
cliParser.add_argument('-B', '--byte-limit', metavar='<N>',
        dest='byteLimit',
        default=DEFAULT_BYTE_LIMIT,
        type=int,
        help='maximum bytes read at one time per socket (default {:d}'.format(DEFAULT_BYTE_LIMIT))
cliParser.add_argument('-H', '--listen-host', metavar='<HOSTNAME>',
        dest='listenHost',
        default='127.0.0.1',
        help='the client-facing TCP proxy should bind to this interface (default 127.0.0.1; use 0.0.0.0 for all interfaces)')
cliParser.add_argument('-p', '--listen-port', metavar='<N>',
        dest='listenPort',
        default=0,
        type=int,
        help='the client-facing TCP proxy port (default 0 implies a random port is chosen)')
cliParser.add_argument('-g', '--group', '--workgroup', metavar='<WORKGROUP>',
        dest='workgroup',
        default=None,
        help='the workgroup used to submit the vscode job')
cliParser.add_argument('-S', '--salloc-arg', metavar='<SLURM-ARG>',
        dest='sallocArgs',
        action='append',
        help='used zero or more times to specify arguments to the salloc command being wrapped (e.g. --partition=<name>, --ntasks=<N>)')

cliArgs = cliParser.parse_args()

# Figure the logging level:
chosenLoggingLevel = min(max(0, baseLoggingLevel + cliArgs.verbosity - cliArgs.quietness), len(loggingLevels)-1)
if cliArgs.logFile:
    cliArgs.logFile = cliArgs.logFile.replace('[PID]', str(os.getpid()))
logging.basicConfig(filename=cliArgs.logFile, level=loggingLevels[chosenLoggingLevel], format='%(asctime)s [%(levelname)s] %(message)s')

# Get tee files opened:
teeFiles = { 'stdin': None, 'stdout': None, 'stderr': None }
if cliArgs.teeStdinFile:
    teeFiles['stdin'] = open(cliArgs.teeStdinFile.replace('[PID]', str(os.getpid())), 'w')
if cliArgs.teeStdoutFile:
    teeFiles['stdout'] = open(cliArgs.teeStdoutFile.replace('[PID]', str(os.getpid())), 'w')
if cliArgs.teeStderrFile:
    teeFiles['stderr'] = open(cliArgs.teeStderrFile.replace('[PID]', str(os.getpid())), 'w')

# If no workgroup was provided, find one for this user:
if cliArgs.workgroup is None:
    logging.debug('Looking-up a workgroup for the current user')
    workgroupLookupProc = subprocess.Popen(['workgroup', '-q', 'workgroups'],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True
                            )
    (workgroupStdout, dummy) = workgroupLookupProc.communicate()
    # Extract the left-most <gid> <gname> pair:
    workgroupMatch = re.match(r'^\s*[0-9]+\s*(\S+)', workgroupStdout)
    if workgroupMatch is None:
        logging.critical('No workgroup provided and user appears to be a member of no workgroups')
        exit(errno.EINVAL)
    cliArgs.workgroup = workgroupMatch.group(1)
    logging.info('Automatically selected workgroup %s', cliArgs.workgroup)

# Run the proxy server:
asyncio.run(runloop())
