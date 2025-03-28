#
# University of Delaware
# DARWIN Cluster site configuration script
#

import errno

class VSCodeProxyConfigUDelDARWIN(VSCodeProxyConfig):
    """Configuration subclass that adds functionality necessary for the University of Delaware DARWIN cluster."""
    
    # We use the Slurm job scheduler and the `salloc` command to start interactive jobs:
    SCHEDULER_NAME: str = 'salloc'

    @classmethod
    def cli_parser(cls):
        """Add the workgroup CLI option to the program so we know under which workgroup the salloc should be executed."""
        cli_parser = super().cli_parser()
        cli_parser.add_argument('-g', '--group', '--workgroup', metavar='<WORKGROUP>',
            dest='workgroup',
            default=None,
            help='the workgroup used to submit the vscode job')
        return cli_parser
    
    def get_workgroup(self) -> 'str|None':
        """Support function that determines the workgroup that should be used to submit the job, based on user input or (lacking that) the first workgroup displayed by the `workgroup` command for the user."""
        if not self.workgroup:
            log_debug('Looking-up a workgroup for the current user')
            try:
                wg_lookup_proc = subprocess.Popen(['workgroup', '-q', 'workgroups'],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    text=True,
                                    universal_newlines=True
                                )
                (wg_stdout, _) = wg_lookup_proc.communicate()
            
                # Extract the left-most <gid> <gname> pair:
                m = re.match(r'^\s*[0-9]+\s*(\S+)', wg_stdout)
                if m is None:
                    log_crit('No workgroup provided and user appears to be a member of no workgroups')
                    sys.exit(errno.EINVAL)
                self.workgroup = m.group(1)
                log_info('Automatically selected workgroup %s', self.workgroup)
            except Exception as e:
                log_crit('Could not lookup default workgroup: %s', str(e))
                sys.exit(errno.EINVAL)
        return self.workgroup

VSCodeProxyConfigClass = VSCodeProxyConfigUDelDARWIN

#
# We don't need a customized binary TCP proxy class.
#

class VSCodeBackendLauncherUDelDARWIN(VSCodeBackendLauncher):
    """Backend launcher subclass that adds functionality necessary for the University of Delaware DARWIN cluster."""

    def job_scheduler_base_cmd(self) -> List[str]:
        """The `salloc` command gets wrapped by the `workgroup` command so that the correct workgroup is selected."""
        return ['workgroup', '-g', self._cfg.get_workgroup(), '--command', '@', '--', 'salloc' ]

VSCodeBackendLauncherClass = VSCodeBackendLauncherUDelDARWIN
