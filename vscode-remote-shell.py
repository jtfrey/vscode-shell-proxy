#!/usr/bin/env python

import socket
import threading
import select
try:
    from Queue import Queue, Empty as QueueEmpty
except:
    from queue import Queue, Empty as QueueEmpty
import subprocess
import logging
import time
import argparse
import re
import sys, os, fcntl, errno


if sys.version_info.major > 2:
    def decodeBytes(b, codec='UTF-8'):
        return b.decode(codec=codec)
    def encodeStr(s, codec='UTF-8'):
        return s.encode(codec=codec)
else:
    def decodeBytes(b, codec='UTF-8'): return b
    def encodeStr(s, codec='UTF-8'): return s


DEFAULT_BYTE_LIMIT = 4096
DEFAULT_BACKLOG = 8


try:
    # If the stdin object has a *buffer* attribute, then we will do
    # binary i/o through that:
    dummy = sys.stdin.buffer
    def stdioReadBytes(fptr, byteLimit=DEFAULT_BYTE_LIMIT):
        return fptr.buffer.readline()
    def stdioWriteBytes(fptr, byteObj):
        fptr.buffer.write(byteObj)
except:
    # Otherwise (e.g. Python 2.7) the object's read/write functions will
    # suffice:
    def stdioReadBytes(fptr, byteLimit=DEFAULT_BYTE_LIMIT):
        return fptr.readline()
    def stdioWriteBytes(fptr, byteObj):
        fptr.write(byteObj)
        fptr.flush()



class BaseSocket(object):
    """The BaseSocket class provides the generalized combined socket/file data transfer functionality.  If an instance is given a *codec* at instantiation then binary data read from the socket/file will be coverted to a string with that coding and data for writing that is in string form will be encoded using that codec."""
    
    def __init__(self, sock, codec=None, doNotClose=False):
        self.sock = sock
        self._codec = codec
        self._doNotClose = doNotClose
        
    def __del__(self):
        if not self._doNotClose:
            if type(self.sock) is socket.socket and self.sock.fileno() >= 0:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except:
                    pass
            self.sock.close()
    
    def codec(self): return self._codec
    
    def fileno(self): return self.sock.fileno()
    
    def receiveBytes(self, byteLimit=DEFAULT_BYTE_LIMIT):
        logging.info('-> Read from {:s}'.format(str(self.sock)))
        if type(self.sock) is socket.socket:
            byteObj = self.sock.recv(byteLimit)
        else:
            byteObj = stdioReadBytes(self.sock, byteLimit=byteLimit)
        if self._codec is not None:
            byteObj = decodeBytes(byteObj, codec=self._codec)
        logging.info('<- Read {:d} bytes from {:s}'.format(len(byteObj), str(self.sock)))
        return byteObj
    
    def sendBytes(self, byteObj):
        logging.info('-> Write to {:s}'.format(str(self.sock)))
        if type(byteObj) is not bytes and self._codec is not None:
            byteObj = encodeStr(byteObj, codec=self._codec)
        if type(self.sock) is socket.socket:
            self.sock.send(byteObj)
        else:
            stdioWriteBytes(self.sock, byteObj)
        logging.info('<- Write to {:s}'.format(str(self.sock)))


class QueueBuffer(object):
    """Buffer chunks of binary data in a priority queue.  Lock-based access to the queue conforms to the context protocol."""

    def __init__(self):
        self._buffer = Queue()
    
    def __enter__(self):
        return self._buffer
    
    def __exit__(self, type, value, traceback):
        pass
    
    def hasData(self):
        return not self._buffer.empty()

    def pushData(self, byteObj, **kwargs):
        if byteObj:
            self._buffer.put(byteObj)
    
    def popData(self, **kwargs):
        try:
            byteObj = self._buffer.get_nowait()
        except QueueEmpty:
            byteObj = None
        return byteObj


class StringBuffer(object):
    """Buffer chunks of binary/string data as a string.  Lock-based access to the string conforms to the context protocol."""

    def __init__(self, logToFile=None):
        self._buffer = ''
        self._logToFile = logToFile
        self._dataLock = threading.RLock()
    
    def __enter__(self):
        self._dataLock.acquire()
        return self._buffer
    
    def __exit__(self, type, value, traceback):
        self._dataLock.release()
    
    def hasData(self):
        with self._dataLock:
            return len(self._buffer) > 0
    
    def resetBuffer(self):
        with self._dataLock:
            self._buffer = ''
    
    def logToFile(self): return self._logToFile
    def setLogToFile(self, logToFile=None):
        self._logToFile = logToFile
    
    def pushData(self, byteObj, codec='UTF-8'):
        if byteObj:
            with self._dataLock:
                if type(byteObj) is bytes:
                    byteObj = decodeBytes(byteObj, codec=codec)
                self._buffer += byteObj
                if self._logToFile:
                    self._logToFile.write(byteObj)
                    self._logToFile.flush()
    
    def popData(self, codec='UTF-8'):
        byteObj = None
        with self._dataLock:
            if len(self._buffer):
                byteObj = encodeStr(self._buffer, codec=codec)
                self._buffer = ''
        return byteObj


class BaseBufferedSocket(BaseSocket):
    """Abstract subclass of BaseSocket that uses a buffer object to store bytes as they are read.  A buffer object can be associated with the instance such that writes will remove data from the buffer for sending."""

    @classmethod
    def newBufferObject(cls):
        raise NotImplementedError('{:s} is not a concrete class'.format(cls.__name__))
        
    def __init__(self, sock, **kwargs):
        super(BaseBufferedSocket, self).__init__(sock, **kwargs)
        self._readBuffer = type(self).newBufferObject()
        self._writeBuffer = type(self).newBufferObject()
    
    def readBuffer(self): return self._readBuffer
    
    def writeBuffer(self): return self._writeBuffer
    def setWriteBuffer(self, writeBuffer=None):
        self._writeBuffer = writeBuffer
    
    def receiveBytes(self, byteLimit=DEFAULT_BYTE_LIMIT):
        newData = super(BaseBufferedSocket, self).receiveBytes(byteLimit)
        if newData:
            self._readBuffer.pushData(newData, codec=self.codec())
    
    def sendBytes(self, byteObj=None):
        if byteObj is None and self._writeBuffer is not None:
            byteObj = self._writeBuffer.popData(codec=self.codec())
        if byteObj is not None:
            super(BaseBufferedSocket, self).sendBytes(byteObj)


class QueueBufferedSocket(BaseBufferedSocket):
    """A socket that pushes read data into a Queue."""

    @classmethod
    def newBufferObject(cls):
        return QueueBuffer()
    

class StringBufferedSocket(BaseBufferedSocket):
    """A socket that accumulates read data in a string."""

    @classmethod
    def newBufferObject(cls):
        return StringBuffer()
        
    def __init__(self, sock, logToFile=None, **kwargs):
        super(StringBufferedSocket, self).__init__(sock, **kwargs)
        self.readBuffer().setLogToFile(logToFile=logToFile)


def threadedStdio(S):
    while True:
        try:
            logging.debug('Read line from {:s}'.format(str(S.sock)))
            S.readBuffer().pushData(S.sock.readline())
        except Exception as E:
            logging.debug('Read line from {:s} failed: {:s}'.format(str(S.sock), str(E)))
        

loggingLevels = [ logging.CRITICAL, logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG ]
baseLoggingLevel = 1
        
cliParser = argparse.ArgumentParser(description='vscode cluster proxy')
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
        help='direct all logging to this file rather than stderr')
cliParser.add_argument('--tee-stdin', metavar='<PATH>',
        dest='teeStdinFile',
        default=None,
        help='send a copy of input to the script stdin to this file')
cliParser.add_argument('--tee-stdout', metavar='<PATH>',
        dest='teeStdoutFile',
        default=None,
        help='send a copy of output to the script stdout to this file')
cliParser.add_argument('--tee-stderr', metavar='<PATH>',
        dest='teeStderrFile',
        default=None,
        help='send a copy of output to the script stderr to this file')
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
logging.basicConfig(filename=cliArgs.logFile, level=loggingLevels[chosenLoggingLevel], format='%(asctime)s [%(levelname)s] %(message)s')

# Get tee files opened:
teeStdinFptr = open(cliArgs.teeStdinFile, 'w') if cliArgs.teeStdinFile else None
teeStdoutFptr = open(cliArgs.teeStdoutFile, 'w') if cliArgs.teeStdoutFile else None
teeStderrFptr = open(cliArgs.teeStderrFile, 'w') if cliArgs.teeStderrFile else None

# Standard i/o wrappers:
stdinSock = StringBufferedSocket(sys.stdin, logToFile=teeStdinFptr, codec='UTF-8', doNotClose=True)
stdoutSock = StringBufferedSocket(sys.stdout, logToFile=teeStdoutFptr, codec='UTF-8', doNotClose=True)
stderrSock = StringBufferedSocket(sys.stderr, logToFile=teeStderrFptr, codec='UTF-8', doNotClose=True)

# If no workgroup was provided, find one for this user:
if cliArgs.workgroup is None:
    logging.debug('Looking-up a workgroup for the current user')
    workgroupLookupProc = subprocess.Popen(['workgroup', '-q', 'workgroups'],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE
                            )
    (workgroupStdout, dummy) = workgroupLookupProc.communicate()
    # Extract the left-most <gid> <gname> pair:
    workgroupMatch = re.match(r'^\s*[0-9]+\s*(\S+)', workgroupStdout)
    if workgroupMatch is None:
        logging.critical('No workgroup provided and user appears to be a member of no workgroups')
        exit(errno.EINVAL)
    cliArgs.workgroup = workgroupMatch.group(1)
    logging.info('Automatically selected workgroup %s', cliArgs.workgroup)

# Start the remote shell process:
remoteShellCmd = ['workgroup', '-g', cliArgs.workgroup, '--command', '@', '--', 'salloc' ]
if cliArgs.sallocArgs: remoteShellCmd.extend(cliArgs.sallocArgs)
logging.debug('Command to launch remote shell: "%s"', ' '.join(remoteShellCmd))
remoteShellProc = subprocess.Popen(
                            remoteShellCmd,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE
                        )
logging.info('Remote shell launched with pid %d', remoteShellProc.pid)

# Prepare for processing data on the remote shell stdout and make it non-blocking:
remoteShellStdout = StringBufferedSocket(remoteShellProc.stdout, doNotClose=True)

# Plumb the stdin connection to the remote shell:
remoteShellStdin = StringBufferedSocket(remoteShellProc.stdin, doNotClose=True)
remoteShellStdin.setWriteBuffer(stdinSock.readBuffer())

# Create the wrapper around the remote shell stderr and hook its read buffer into the
# write buffer of the script's stderr:
remoteShellStderr = StringBufferedSocket(remoteShellProc.stderr, doNotClose=True)
stderrSock.setWriteBuffer(remoteShellStderr.readBuffer())

remoteHost = None
hostRegex = re.compile(r'^.*HOSTNAME=(\S*)\s*$')

remotePort = None
portRegex = re.compile(r'^([^0-9]*listeningOn=[^0-9]*)([0-9][0-9]*)([^0-9]*)$')

# For easily mapping select.select() fd's with the socket objects:
fdToSocket = {
        stdoutSock.fileno(): stdoutSock,
        stderrSock.fileno(): stderrSock,
        remoteShellStdin.fileno(): remoteShellStdin
    }
fwdSocketPairs = {}

# Socket to forward TCP traffic to the remote host:
listenSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listenSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listenSocket.setblocking(False)
listenSocket.bind(('', cliArgs.listenPort))
listenPort = listenSocket.getsockname()[1]
isListeningProxyOn = False
logging.info('Preallocated proxy socket on port {:d}'.format(listenPort))

# Pack-up the file descriptors to watch:
checkReads = set()#[stdinSock.fileno(), remoteShellStdout.fileno(), remoteShellStderr.fileno()])
checkWrites = set() #[remoteShellStdin.fileno(), stdoutSock.fileno(), stderrSock.fileno()])
checkXcepts = checkReads | checkWrites

# Process stdin in a separate thread:
stdinThread = threading.Thread(target=threadedStdio, args=(stdinSock,))
stdinThread.daemon = True
stdinThread.start()

# Process remote stdout in a separate thread:
remoteStdoutThread = threading.Thread(target=threadedStdio, args=(remoteShellStdout,))
remoteStdoutThread.daemon = True
remoteStdoutThread.start()

# Process remote stderr in a separate thread:
remoteStderrThread = threading.Thread(target=threadedStdio, args=(remoteShellStderr,))
remoteStderrThread.daemon = True
remoteStderrThread.start()

while True:
    # Is the shell still running?
    logging.debug('Poll for remote shell process status...')
    if remoteShellProc.poll() is not None:
        break
        
    # Check for i/o waiting:
    logging.debug('Waiting for available i/o via select...')
    readWaiting, writeWaiting, xceptWaiting = select.select(checkReads, checkWrites, checkXcepts, 1.0)
    logging.debug('...done.')
    
    # Handle reads first:
    if readWaiting: logging.debug('Read waiting: {:s}'.format(str(readWaiting)))
    for fd in readWaiting:
        if fd == listenSocket:
            # Accept the connection:
            acceptedSock, acceptedAddr = fd.accept()
            logging.info('Accepted connection from {:s}:{:d} (fd={:d})'.format(acceptedAddr[0], acceptedAddr[1], acceptedSock.fileno()))
            
            # Wrap the accepted socket with a queue worker:
            incomingSocket = QueueBufferedSocket(acceptedSock)
            fdToSocket[acceptedSock] = incomingSocket
            
            # Add it to the select fd lists:
            checkReads.add(acceptedSock)
            checkWrites.add(acceptedSock)
            checkXcepts.add(acceptedSock)
            
            logging.debug('Incoming TCP socket {:s}:{:d} added to data queue'.format(acceptedAddr[0], acceptedAddr[1]))
            
            # Open a connection to the remote port:
            while True:
                try:
                    logging.info('Creating remote-facing proxy socket')
                    outgoingSock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    logging.info('Trying to connect to {:s}:{:d}'.format(remoteHost, remotePort))
                    outgoingSock.create_connection((remoteHost, remotePort))
                    logging.info('Connected to {:s}:{:d}'.format(remoteHost, remotePort))
                    outgoingSock.setblocking(False)
                    outgoingSocket = QueueBufferedSocket(outgoingSock)
                    fdToSocket[outgoingSock] = outgoingSocket
                    break
                except Exception as E:
                    logging.error('Failed to connect proxy TCP socket to {:s}:{:d}: {:s}'.format(remotehost, remotePort, str(E)))
            
            # Add it to the select fd lists:
            checkReads.add(outgoingSock)
            checkWrites.add(outgoingSock)
            checkXcepts.add(outgoingSock)
            
            sockName = outgoingSock.getsockname()
            logging.debug('Proxy TCP socket {:s}:{:d} <=> {:s}:{:d} added to data queue'.format(sockName[0], sockName[1], remoteHost, remotePort))
            
            # Cross-link the sockets' read buffers:
            incomingSocket.setWriteBuffer(outgoingSocket.readBuffer())
            outgoingSocket.setWriteBuffer(incomingSocket.readBuffer())
            
            # Note the pairings, too:
            fwdSocketPairs[incomingSocket] = outgoingSocket
            fwdSocketPairs[outgoingSocket] = incomingSocket
            
            # Log some info about the proxy:
            logging.info('-> Proxied connection completed')
            
        elif fd in fdToSocket:
            fdToSocket[fd].receiveBytes(byteLimit=cliArgs.byteLimit)
    
    if not isListeningProxyOn and (remoteHost is None or remotePort is None):
        with remoteShellStdout.readBuffer() as logThusFar:
            if logThusFar:
                # Go line-by-line:
                ignoreLine = False
                doNotReset = False
                for nextLine in logThusFar.splitlines(True):
                    # Once we've found an incomplete line, just commit whatever is left back
                    # to the buffer w/o looking at it:
                    if ignoreLine:
                        remoteShellStdout.readBuffer().pushData(nextLine)
                        continue
                    if not nextLine.endswith('\n'):
                        # We'll end here, reset the buffer and add whatever is left:
                        remoteShellStdout.readBuffer().resetBuffer()
                        remoteShellStdout.readBuffer().pushData(nextLine)
                        ignoreLine = True
                        continue
            
                    # Is it the HOSTNAME line from the env?
                    if remoteHost is None and 'HOSTNAME=' in nextLine:
                        logging.debug('Found hostname line: {:s}'.format(nextLine))
                        hostMatch = hostRegex.search(nextLine)
                        if hostMatch is not None:
                            remoteHost = hostMatch.group(1)
                
                    # Is it the listening port line?
                    if remotePort is None and 'listeningOn=' in nextLine:
                        logging.debug('Found listening on line: {:s}'.format(nextLine))
                        portMatch = portRegex.search(nextLine)
                        if portMatch is not None:
                            remotePort = int(portMatch.group(2))
                            nextLine = re.sub(portRegex, r'\g<1>{:d}\g<3>'.format(listenPort), nextLine)
            
                    # If we now have the remote host and port, we can begin the tunneling:
                    if not isListeningProxyOn and remoteHost is not None and remotePort is not None:
                        # Start listening on the forwarding socket:
                        listenSocket.listen(cliArgs.backlog)
                        isListeningProxyOn = True
                    
                        # The listener will be a special case -- just a plain
                        # socket fd:
                        checkReads.add(listenSocket)
                        checkXcepts.add(listenSocket)
                        logging.info('Listening for TCP forwarding on {:d}'.format(listenPort))
                    
                        # We need to link the remote stdout read buffer to the stdout write
                        # buffer now.  But we've been adding to the stdout write buffer...so
                        # we'll reset the remote stdout buffer and copy what was in the stdout
                        # write buffer to it, then link them:
                        remoteShellStdout.readBuffer().resetBuffer()
                        remoteShellStdout.readBuffer().pushData(stdoutSock.writeBuffer().popData())
                        stdoutSock.setWriteBuffer(remoteShellStdout.readBuffer())
                        doNotReset = True
            
                    # Add the line to the outgoing stdout:
                    stdoutSock.writeBuffer().pushData(nextLine)
                    
                logging.debug('Waiting data:  {:s}'.format(stdoutSock.writeBuffer()._buffer))
        
                if not doNotReset and not ignoreLine:
                    remoteShellStdout.readBuffer().resetBuffer()
    
    # Handle writes next:
    for fd, sockObj in fdToSocket.items():
        sockBuffer = sockObj.writeBuffer()
        if sockBuffer is not None and sockBuffer.hasData():
            sockObj.sendBytes()

