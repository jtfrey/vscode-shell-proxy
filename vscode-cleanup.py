#!/usr/bin/env python
#
# Find "vscode" process trees that are older than some age and optionally
# kill them.
#

import subprocess
import time
import signal
import sys
import os
import re
import argparse


def childPids(ppid, pidSet=set()):
    """Recursively find all children processes of the given *ppid*"""
    psProc = subprocess.Popen(
                        ['pgrep', '--parent', str(ppid)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
    (psOut, psErr) = psProc.communicate()
    del psProc
    for psLine in psOut.splitlines():
        pid = int(psLine)
        pidSet.add(pid)
        childPids(pid, pidSet)
    return childPids


def parseDurationString(durationStr):
    """Parse a duration string and return the number of seconds represented.  Units of d/h/m/s are permissible, e.g. 5d6h10m45s."""
    unitMultiplier = {
            'd': 86400, 'D': 86400,
            'h': 3600,  'H': 3600,
            'm': 60,    'M': 60,
            's': 1,     'S': 1
        }
    chunkRegex = re.compile(r'^\s*([0-9]+)([dhmsDHMS]?)(.*)\s*$')
    seconds = 0
    try:
        durS = durationStr
        while durS:
            chunkMatch = chunkRegex.match(durS)
            if chunkMatch is None:
                break
            dt = int(chunkMatch.group(1))
            if chunkMatch.group(2):
                dt *= unitMultiplier[chunkMatch.group(2)]
            durS = chunkMatch.group(3)
            seconds += dt
    except:
        raise ValueError('Invalid age string: {:s}'.format(durationStr))
    return seconds


cliParser = argparse.ArgumentParser(description='find all processes that appear to be Visual Studio Code remote sessions')
cliParser.add_argument('-q', '--quiet',
        dest='isNotQuiet',
        default=True,
        action='store_false',
        help='do not output per-pid info as the program runs')
cliParser.add_argument('-k', '--kill',
        dest='shouldKill',
        default=False,
        action='store_true',
        help='in addition to printing the process ids also send SIGKILL to each')
cliParser.add_argument('-a', '--age', metavar='<N>{dhms}{<N>{dhms}..}',
        dest='age',
        default='5d',
        help='processes older than this value will be eligible for destruction; a bare number is implied to be seconds, more complex values can use the d/h/m/s characters as units (default 5d)')
        
cliArgs = cliParser.parse_args()

# Figure out delta-t for age comparisons:
dt = parseDurationString(cliArgs.age)
thresholdTime = int(time.time()) - dt

# Find all processes with the text 'vscode-server' in their command line:
try:
    psProc = subprocess.Popen(
                        ['pgrep', '-f', 'vscode-server'],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
    (psOut, psErr) = psProc.communicate()
    del psProc
except Exception as E:
    sys.stderr.write('ERROR:  pgrep of vscode processes failed: {:s}'.format(str(E)))
    exit(1)

# The pgrep command will produce one pid per line:
pidsOfInterest = set()
for psLine in psOut.splitlines():
    pid = int(psLine)
    # Add this pid and all children to the set:
    pidsOfInterest.add(pid)
    childPids(pid, pidsOfInterest)

# Show/kill any pids older than the given temporal threshold:
exitCode = 0
for pid in pidsOfInterest:
    # Get the process start time by grabbing the creation date off
    # its directory in /proc:
    try:
        pidFInfo = os.stat('/proc/{:d}'.format(pid))
    except:
        continue
        
    if pidFInfo.st_ctime < thresholdTime:
        if cliArgs.shouldKill:
            try:
                os.kill(pid, signal.SIGKILL)
                rc = 'KILLED'
            except Exception as E:
                rc = 'FAILED ({:s})'.format(str(E))
                exitCode = 1
            if cliArgs.isNotQuiet: print('{:d} {:s}'.format(pid, rc))
        else:
            if cliArgs.isNotQuiet: print(pid)

exit(exitCode)
