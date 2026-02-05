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
    
    def __init__(self, **kwargs):
        """Override the initializer to add the VSCODE env vars by default."""
        super().__init__(**kwargs)
        if not self.scheduler_envs:
            self.scheduler_envs = list()
        self.scheduler_envs.extend([
                    'VSCODE_SERVER_CUSTOM_GLIBC_PATH=/opt/shared/crosstool-ng/sysroots/x86_64-gcc-8.5.0-glibc-2.28/lib',
                    'VSCODE_SERVER_CUSTOM_GLIBC_LINKER=/opt/shared/crosstool-ng/sysroots/x86_64-gcc-8.5.0-glibc-2.28/lib/ld-linux-x86-64.so.2',
                    'VSCODE_SERVER_PATCHELF_PATH=/opt/shared/patchelf/0.18.0/bin/patchelf'
                ])
    
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
        import pwd, os
        job_name = 'vscode-remotessh-' + pwd.getpwuid(os.geteuid()).pw_name
        return ['workgroup', '-g', self._cfg.get_workgroup(), '--command', '@', '--', 'salloc', '--job-name='+job_name ]

    def get_job_scheduler_cmd(self) -> List[str]:
        """Synthesize the appropriate `srun` command with any env vars exported to the remote shell environment."""
        cmd = super().get_job_scheduler_cmd()
        envs = 'TERM'
        if self._cfg.scheduler_envs:
            for env_spec in self._cfg.scheduler_envs:
                e, _ = env_spec.split('=', 1) if '=' in env_spec else (env_spec, '')
                envs += ',' + e
        cmd.extend(('srun', '--mpi=none', '--pty', '--cpu-bind=none', '--export='+envs, '$SHELL', '-l'))
        return cmd

VSCodeBackendLauncherClass = VSCodeBackendLauncherUDelDARWIN
