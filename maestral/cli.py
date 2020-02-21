#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Nov 30 13:51:32 2018

@author: samschott

This file defines the functions to configure and interact
with Maestral from the command line.

We aim to import most packages locally in the functions that required them,
in order to reduce the startup time of individual CLI commands.

"""
# system imports
import os
import functools
import logging
import platform

# external packages
import click
import Pyro5.errors

# local imports
from maestral.config import list_configs

OK = click.style('[OK]', fg='green')
FAILED = click.style('[FAILED]', fg='red')
KILLED = click.style('[KILLED]', fg='red')


def pending_link_cli(config_name):
    """
    This does not create a Maestral instance and is therefore safe to call from anywhere
    at any time.
    """
    from maestral.utils import pending_link
    from keyring.errors import KeyringLocked

    try:
        if pending_link(config_name):
            click.echo('No Dropbox account linked.')
            return True
        else:
            return False
    except KeyringLocked:
        click.echo('Error: Cannot access user keyring to load Dropbox credentials.')


def start_daemon_subprocess_with_cli_feedback(config_name):
    """Wrapper around `daemon.start_maestral_daemon_process`
    with command line feedback."""
    from maestral.daemon import start_maestral_daemon_process, Start

    click.echo('Starting Maestral...', nl=False)
    res = start_maestral_daemon_process(config_name)
    if res == Start.Ok:
        click.echo('\rStarting Maestral...        ' + OK)
    else:
        click.echo('\rStarting Maestral...        ' + FAILED)


def stop_daemon_with_cli_feedback(config_name):
    """Wrapper around `daemon.stop_maestral_daemon_process`
    with command line feedback."""

    from maestral.daemon import stop_maestral_daemon_process, Exit

    click.echo('Stopping Maestral...', nl=False)
    res = stop_maestral_daemon_process(config_name)
    if res == Exit.Ok:
        click.echo('\rStopping Maestral...        ' + OK)
    elif res == Exit.NotRunning:
        click.echo('Maestral daemon is not running.')
    elif res == Exit.Killed:
        click.echo('\rStopping Maestral...        ' + KILLED)


def _check_for_updates():
    """Checks if updates are available by reading the cached release number from the
    config file and notifies the user."""
    from maestral import __version__
    from maestral.config import MaestralState
    from maestral.utils.updates import check_version

    state = MaestralState('maestral')
    latest_release = state.get('app', 'latest_release')

    has_update = check_version(__version__, latest_release, '<')

    if has_update:
        click.secho(
            'Maestral v{0} has been released, you have v{1}. Please use your '
            'package manager to update.'.format(latest_release, __version__), fg='red'
        )


def _check_for_fatal_errors(m):
    """Checks for fatal errors such as revoked Dropbox access,
    deleted Dropbox folder etc."""
    maestral_err_list = m.maestral_errors

    if len(maestral_err_list) > 0:

        import textwrap
        width, height = click.get_terminal_size()

        err = maestral_err_list[0]

        wrapped_msg = textwrap.wrap(err['message'], width=width)

        click.echo('')
        click.secho(err['title'], fg='red')
        click.secho('\n'.join(wrapped_msg), fg='red')
        click.echo('')

        return True
    else:
        return False


def catch_maestral_errors(func):
    """
    Decorator that catches all MaestralApiErrors and prints them as a useful message to
    the user instead of printing the full stacktrace to the console.
    """

    from maestral.errors import MaestralApiError

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except MaestralApiError as exc:
            import textwrap
            width, height = click.get_terminal_size()
            wrapped_msg = textwrap.wrap(exc.message, width=width)

            click.echo('')
            click.secho(exc.title, fg='red')
            click.secho('\n'.join(wrapped_msg), fg='red')
            click.echo('')

    return wrapper


def format_table(columns, headers=None, spacing=2):

    if headers:
        for c, h in zip(columns, headers):
            c.insert(0, h)

    col_widths = tuple(max(len(line) for line in col) + spacing for col in columns)

    n_rows = max(len(c) for c in columns)
    rows = []

    for i in range(n_rows):
        rows.append(''.join(c[i].ljust(w) for c, w in zip(columns, col_widths)))

    return '\n'.join(rows)


# ========================================================================================
# Command groups
# ========================================================================================


class SpecialHelpOrder(click.Group):

    def __init__(self, *args, **kwargs):
        self.help_priorities = {}
        super(SpecialHelpOrder, self).__init__(*args, **kwargs)

    def get_help(self, ctx):
        self.list_commands = self.list_commands_for_help
        return super(SpecialHelpOrder, self).get_help(ctx)

    def list_commands_for_help(self, ctx):
        """reorder the list of commands when listing the help"""
        commands = super(SpecialHelpOrder, self).list_commands(ctx)
        return (c[1] for c in sorted(
            (self.help_priorities.get(command, 1), command)
            for command in commands))

    def command(self, *args, **kwargs):
        """Behaves the same as `click.Group.command()` except capture
        a priority for listing command names in help.
        """
        help_priority = kwargs.pop('help_priority', 1)
        help_priorities = self.help_priorities

        def decorator(f):
            cmd = super(SpecialHelpOrder, self).command(*args, **kwargs)(f)
            help_priorities[cmd.name] = help_priority
            return cmd

        return decorator

    def group(self, *args, **kwargs):
        """Behaves the same as `click.Group.group()` except capture
        a priority for listing command names in help.
        """
        help_priority = kwargs.pop('help_priority', 1)
        help_priorities = self.help_priorities

        def decorator(f):
            cmd = super(SpecialHelpOrder, self).group(*args, **kwargs)(f)
            help_priorities[cmd.name] = help_priority
            return cmd

        return decorator


def _check_and_set_config(ctx, param, value):
    """
    Checks if the selected config name, passed as :param:`value`, is valid.

    :param ctx: Click context to be passed to command.
    :param param: Name of click parameter, in our case 'config_name'.
    :param value: Value  of click parameter, in our case the selected config.
    """

    # check if valid config
    if value not in list_configs() and not value == 'maestral':
        ctx.fail(f'Configuration \'{value}\' does not exist. You can create new '
                 'configuration with \'maestral config add\'.')

    return value


with_config_opt = click.option(
    '-c', '--config-name',
    default='maestral',
    is_eager=True,
    expose_value=True,
    metavar='NAME',
    callback=_check_and_set_config,
    help='Run Maestral with the selected configuration.'
)


@click.group(cls=SpecialHelpOrder)
def main():
    """Maestral Dropbox Client for Linux and macOS."""
    _check_for_updates()


@main.group(cls=SpecialHelpOrder, help_priority=14)
def excluded():
    """View and manage excluded folders."""


@main.group(cls=SpecialHelpOrder, help_priority=16)
def config():
    """Manage different Maestral configuration environments."""


@main.group(cls=SpecialHelpOrder, help_priority=17)
def notify():
    """Manage Desktop notifications."""


@main.group(cls=SpecialHelpOrder, help_priority=19)
def log():
    """View and manage Maestral's log."""


# ========================================================================================
# Main commands
# ========================================================================================

@main.command(help_priority=0)
@with_config_opt
def gui(config_name):
    """Runs Maestral with a GUI."""
    try:
        if platform.system() == 'Darwin':
            from maestral_cocoa.main import run
        else:
            from maestral_qt.main import run
    except ImportError:
        click.echo('No maestral GUI installed. Please run \'pip3 install maestral[gui]\'.')
    else:
        run(config_name)


@main.command(help_priority=1)
@click.option('--foreground', '-f', is_flag=True, default=False,
              help='Starts Maestral in the foreground.')
@with_config_opt
@catch_maestral_errors
def start(config_name: str, foreground: bool):
    """Starts the Maestral as a daemon."""

    from maestral.daemon import get_maestral_pid
    from maestral.utils import pending_dropbox_folder

    # do nothing if already running
    if get_maestral_pid(config_name):
        click.echo('Maestral daemon is already running.')
        return

    from maestral.main import Maestral

    # run setup if not yet done
    if pending_link_cli(config_name) or pending_dropbox_folder(config_name):
        m = Maestral(config_name, run=False)
        m.create_dropbox_directory()
        m.set_excluded_folders()

        m.sync.last_cursor = ''
        m.sync.last_sync = 0

        del m

    # start daemon
    if foreground:
        from maestral.daemon import run_maestral_daemon
        run_maestral_daemon(config_name, run=True, log_to_stdout=True)
    else:
        start_daemon_subprocess_with_cli_feedback(config_name)


@main.command(help_priority=2)
@with_config_opt
def stop(config_name: str):
    """Stops the Maestral daemon."""
    stop_daemon_with_cli_feedback(config_name)


@main.command(help_priority=3)
@click.option('--foreground', '-f', is_flag=True, default=False,
              help='Starts Maestral in the foreground.')
@with_config_opt
@click.pass_context
def restart(ctx, config_name: str, foreground: bool):
    """Restarts the Maestral daemon."""
    stop_daemon_with_cli_feedback(config_name)
    ctx.forward(start)


@main.command(help_priority=4)
@with_config_opt
@click.option('--yes', '-Y', is_flag=True, default=False)
@click.option('--no', '-N', is_flag=True, default=False)
def autostart(config_name: str, yes: bool, no: bool):
    """Start the maestral daemon on log-in."""
    from maestral.utils.autostart import AutoStart
    auto_start = AutoStart(config_name)

    if not auto_start.implementation:
        click.echo('Autostart is currently not supported for your platform.\n'
                   'Autostart requires systemd on Linux or launchd on macOS.')
        return

    if yes or no:
        auto_start.enabled = yes
        enabled_str = 'Enabled' if yes else 'Disabled'
        click.echo(f'{enabled_str} start on login.')
    else:
        enabled_str = 'enabled' if auto_start.enabled else 'disabled'
        click.echo(f'Autostart is {enabled_str}.')


@main.command(help_priority=5)
@with_config_opt
def pause(config_name: str):
    """Pauses syncing."""
    from maestral.daemon import MaestralProxy

    try:
        with MaestralProxy(config_name) as m:
            m.pause_sync()
        click.echo('Syncing paused.')
    except Pyro5.errors.CommunicationError:
        click.echo('Maestral daemon is not running.')


@main.command(help_priority=6)
@with_config_opt
def resume(config_name: str):
    """Resumes syncing."""
    from maestral.daemon import MaestralProxy

    try:
        with MaestralProxy(config_name) as m:
            if not _check_for_fatal_errors(m):
                m.resume_sync()
                click.echo('Syncing resumed.')

    except Pyro5.errors.CommunicationError:
        click.echo('Maestral daemon is not running.')


@main.command(help_priority=7)
@with_config_opt
def status(config_name: str):
    """Returns the current status of the Maestral daemon."""
    from maestral.daemon import MaestralProxy

    try:
        with MaestralProxy(config_name) as m:

            n_errors = len(m.sync_errors)
            color = 'red' if n_errors > 0 else 'green'
            n_errors_str = click.style(str(n_errors), fg=color)
            click.echo('')
            click.echo('Account:       {}'.format(m.get_state('account', 'email')))
            click.echo('Usage:         {}'.format(m.get_state('account', 'usage')))
            click.echo('Status:        {}'.format(m.status))
            click.echo('Sync errors:   {}'.format(n_errors_str))
            click.echo('')

            _check_for_fatal_errors(m)

            sync_err_list = m.sync_errors

            if len(sync_err_list) > 0:
                header = ('PATH', 'ERROR')
                col0 = list('\'{}\''.format(err['dbx_path']) for err in sync_err_list)
                col1 = list('{}. {}'.format(err['title'], err['message']) for err in sync_err_list)

                click.echo(format_table([col0, col1], header, spacing=4))
                click.echo('')

    except Pyro5.errors.CommunicationError:
        click.echo('Maestral daemon is not running.')


@main.command(help_priority=8)
@click.argument('local_path', type=click.Path(exists=True))
@with_config_opt
def file_status(config_name: str, local_path: str):
    """Returns the current sync status of a given file or folder."""
    from maestral.daemon import MaestralProxy

    try:
        with MaestralProxy(config_name) as m:

            if _check_for_fatal_errors(m):
                return

            stat = m.get_file_status(local_path)
            click.echo(stat)

    except Pyro5.errors.CommunicationError:
        click.echo('unwatched')


@main.command(help_priority=9)
@with_config_opt
def activity(config_name: str):
    """Live view of all items being synced."""
    from maestral.daemon import MaestralProxy

    try:
        with MaestralProxy(config_name) as m:

            if _check_for_fatal_errors(m):
                return

            import curses
            import time

            def curses_loop(screen):

                curses.use_default_colors()  # don't change terminal background
                screen.nodelay(1)  # set `scree.getch()` to non-blocking

                while True:

                    # get info from daemon
                    res = m.get_activity()
                    up = res['uploading']
                    down = res['downloading']
                    sync_status = m.status
                    n_errors = len(m.sync_errors)

                    # create header
                    lines = [
                        f'Status: {sync_status}, Sync errors: {n_errors}',
                        f'Uploading: {len(up)}, Downloading: {len(down)}',
                        '',
                    ]

                    # create table
                    up.insert(0, ('UPLOADING', 'STATUS'))  # column titles
                    up.append(('', ''))  # append spacer
                    down.insert(0, ('DOWNLOADING', 'STATUS'))  # column titles

                    file_names = tuple(os.path.basename(item[0]) for item in up + down)
                    states = tuple(item[1] for item in up + down)
                    col_len = max(len(fn) for fn in file_names) + 2

                    for fn, s in zip(file_names, states):  # create rows
                        lines.append(fn.ljust(col_len) + s)

                    # print to console screen
                    screen.clear()
                    try:
                        screen.addstr('\n'.join(lines))
                    except curses.error:
                        pass
                    screen.refresh()

                    # abort when user presses 'q', refresh otherwise
                    key = screen.getch()
                    if key == ord('q'):
                        break
                    elif key < 0:
                        time.sleep(1)

            # enter curses event loop
            curses.wrapper(curses_loop)

    except Pyro5.errors.CommunicationError:
        click.echo('Maestral daemon is not running.')


@main.command(help_priority=910)
@with_config_opt
@click.argument('dropbox_path', type=click.Path(), default='')
@catch_maestral_errors
def ls(dropbox_path: str, config_name: str):
    """Lists contents of a Dropbox directory."""

    if not dropbox_path.startswith('/'):
        dropbox_path = '/' + dropbox_path

    if not pending_link_cli(config_name):
        from maestral.daemon import MaestralProxy
        from maestral.errors import PathError

        with MaestralProxy(config_name, fallback=True) as m:
            try:
                entries = m.list_folder(dropbox_path, recursive=False)
            except PathError:
                click.echo(f'Error: No such directory on Dropbox: \'{dropbox_path}\'')
                return

            if not entries:
                click.echo('Could not connect to Dropbox')
                return

            types = list('file' if e['type'] == 'FileMetadata' else 'folder' for e in entries)
            shared_status = list('shared' if 'sharing_info' in e else 'private' for e in entries)
            names = list(e['name'] for e in entries)
            excluded_status = list(m.excluded_status(e['path_lower']) for e in entries)

            click.echo('')
            click.echo(format_table([types, shared_status, names, excluded_status]))
            click.echo('')


@main.command(help_priority=11)
@with_config_opt
@click.option('-r', 'relink', is_flag=True, default=False,
              help='Relink to the current account. Keeps the sync state.')
@catch_maestral_errors
def link(config_name: str, relink: bool):
    """Links Maestral with your Dropbox account."""

    if relink or pending_link_cli(config_name):
        from maestral.oauth import OAuth2Session
        from maestral.daemon import get_maestral_pid

        if get_maestral_pid(config_name):
            click.echo('Maestral is running. Please stop before linking.')
            return

        auth = OAuth2Session(config_name)
        auth.link()

    else:
        click.echo('Maestral is already linked. Use the option '
                   '\'-r\' to relink to the same account.')


@main.command(help_priority=12)
@with_config_opt
@click.confirmation_option(prompt='Are you sure you want unlink your account?')
@catch_maestral_errors
def unlink(config_name: str):
    """Unlinks your Dropbox account."""

    if not pending_link_cli(config_name):

        from maestral.main import Maestral

        stop_daemon_with_cli_feedback(config_name)
        m = Maestral(config_name, run=False)
        m.unlink()

        click.echo('Unlinked Maestral.')


@main.command(help_priority=13)
@with_config_opt
@click.argument('new_path', required=False, type=click.Path(writable=True))
def move_dir(config_name: str, new_path: str):
    """Change the location of your Dropbox folder."""

    if not pending_link_cli(config_name):
        from maestral.main import select_dbx_path_dialog
        from maestral.daemon import MaestralProxy

        new_path = new_path or select_dbx_path_dialog(config_name, allow_merge=False)

        with MaestralProxy(config_name, fallback=True) as m:
            m.move_dropbox_directory(new_path)

        click.echo(f'Dropbox folder moved to {new_path}.')


@main.command(help_priority=15)
@with_config_opt
@catch_maestral_errors
def rebuild_index(config_name: str):
    """Rebuilds Maestral's index. May take several minutes."""

    if not pending_link_cli(config_name):

        import textwrap

        width, height = click.get_terminal_size()

        message1 = textwrap.wrap(
            'If you encounter sync issues, please run \'maestral status\' to check for '
            'sync issues which can should be resolved manually, e.g., incompatible file '
            'names or insufficient permissions. After resolving them, please pause and '
            'resume syncing. Only rebuild the index if you continue to have problems '
            'after taking those steps.',
            width=width,
        )

        message2 = textwrap.wrap(
            'Rebuilding the index may take several minutes, depending on the size of '
            'your Dropbox. Please do not modify any items in your local Dropbox folder '
            'during this process. Any changes to local files while rebuilding may be '
            'lost.',
            width=width
        )

        click.echo('\n'.join(message1) + '\n')
        click.echo('\n'.join(message2) + '\n')
        click.confirm('Do you want to continue?', abort=True)

        import time
        import Pyro5.client
        from concurrent.futures import ThreadPoolExecutor
        from maestral.daemon import MaestralProxy, get_maestral_proxy

        m0 = get_maestral_proxy(config_name, fallback=True)

        def rebuild_in_thread():
            if isinstance(m0, Pyro5.client.Proxy):
                # rebuild index from separate proxy
                with MaestralProxy(config_name) as m1:
                    m1.rebuild_index()
            else:
                # rebuild index with main instance
                m0.rebuild_index()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(rebuild_in_thread)
            while future.running():  # get status updates while rebuilding
                msg = ('\r' + m0.status).ljust(width)
                click.echo(msg, nl=False)
                time.sleep(1.0)

        future.result()  # this will raise any errors during rebuilding
        click.echo('\rRebuilding complete.'.ljust(width))

        del m0  # delete while still in scope

    else:
        click.echo('Maestral does not appear to be linked.')


@main.command(help_priority=18)
@with_config_opt
@click.option('--yes', '-Y', is_flag=True, default=False)
@click.option('--no', '-N', is_flag=True, default=False)
def analytics(config_name: str, yes: bool, no: bool):
    """Enables or disables sharing error reports."""
    # This is safe to call regardless if the GUI or daemon are running.
    from maestral.daemon import MaestralProxy
    from maestral.config.main import MaestralConfig

    if yes or no:
        try:
            with MaestralProxy(config_name) as m:
                m.analytics = yes
        except Pyro5.errors.CommunicationError:
            MaestralConfig(config_name).set('app', 'analytics', yes)

        enabled_str = 'Enabled' if yes else 'Disabled'
        click.echo(f'{enabled_str} automatic error reports.')
    else:
        try:
            with MaestralProxy(config_name) as m:
                state = m.analytics
        except Pyro5.errors.CommunicationError:
            state = MaestralConfig(config_name).get('app', 'analytics')
        enabled_str = 'enabled' if state else 'disabled'
        click.echo(f'Automatic error reports are {enabled_str}.')


@main.command(help_priority=20)
@with_config_opt
def account_info(config_name: str):
    """Prints your Dropbox account information."""

    from maestral.daemon import MaestralProxy

    if not pending_link_cli(config_name):
        with MaestralProxy(config_name, fallback=True) as m:

            email = m.get_state('account', 'email')
            account_type = m.get_state('account', 'type').capitalize()
            usage = m.get_state('account', 'usage')
            path = m.get_conf('main', 'path')

            click.echo('')
            click.echo('Email:             {}'.format(email))
            click.echo('Account-type:      {}'.format(account_type))
            click.echo('Usage:             {}'.format(usage))
            click.echo('Dropbox location:  \'{}\''.format(path))
            click.echo('')


@main.command(help_priority=21)
def about():
    """Returns the version number and other information."""
    import time
    from maestral import __url__
    from maestral import __author__
    from maestral import __version__

    year = time.localtime().tm_year
    click.echo('')
    click.echo('Version:    {}'.format(__version__))
    click.echo('Website:    {}'.format(__url__))
    click.echo('Copyright:  (c) 2018-{0}, {1}.'.format(year, __author__))
    click.echo('')


# ========================================================================================
# Exclude commands
# ========================================================================================

@excluded.command(name='list', help_priority=0)
@with_config_opt
def excluded_list(config_name: str):
    """Lists all excluded folders."""

    if not pending_link_cli(config_name):
        from maestral.daemon import MaestralProxy

        with MaestralProxy(config_name, fallback=True) as m:

            excluded_folders = m.get_conf('main', 'excluded_folders')
            excluded_folders.sort()

            if len(excluded_folders) == 0:
                click.echo('No excluded folders.')
            else:
                for folder in excluded_folders:
                    click.echo(folder)


@excluded.command(name='add', help_priority=1)
@with_config_opt
@click.argument('dropbox_path', type=click.Path())
@catch_maestral_errors
def excluded_add(dropbox_path: str, config_name: str):
    """Adds a folder to the excluded list and re-syncs."""

    if not dropbox_path.startswith('/'):
        dropbox_path = '/' + dropbox_path

    if dropbox_path == '/':
        click.echo(click.style('Cannot exclude the root directory.', fg='red'))
        return

    if not pending_link_cli(config_name):

        from maestral.daemon import MaestralProxy

        with MaestralProxy(config_name, fallback=True) as m:
            if _check_for_fatal_errors(m):
                return
            try:
                m.exclude_folder(dropbox_path)
                click.echo(f'Excluded directory \'{dropbox_path}\'.')
            except ConnectionError:
                click.echo('Could not connect to Dropbox.')
            except ValueError as e:
                click.echo('Error: ' + e.args[0])


@excluded.command(name='remove', help_priority=2)
@with_config_opt
@click.argument('dropbox_path', type=click.Path())
@catch_maestral_errors
def excluded_remove(dropbox_path: str, config_name: str):
    """Removes a folder from the excluded list and re-syncs."""

    if not dropbox_path.startswith('/'):
        dropbox_path = '/' + dropbox_path

    if dropbox_path == '/':
        click.echo(click.style('The root directory is always included.', fg='red'))
        return

    if not pending_link_cli(config_name):

        from maestral.daemon import MaestralProxy

        try:
            with MaestralProxy(config_name) as m:
                if _check_for_fatal_errors(m):
                    return
                try:
                    m.include_folder(dropbox_path)
                    click.echo(f'Included directory \'{dropbox_path}\'. Now downloading...')
                except ConnectionError:
                    click.echo('Could not connect to Dropbox.')
                except ValueError as e:
                    click.echo('Error: ' + e.args[0])

        except Pyro5.errors.CommunicationError:
            click.echo('Maestral daemon must be running to download folders.')


# ========================================================================================
# Log commands
# ========================================================================================

@log.command(name='show', help_priority=0)
@with_config_opt
def log_show(config_name: str):
    """Prints Maestral's logs to the console."""
    from maestral.utils.appdirs import get_log_path

    log_file = get_log_path('maestral', config_name + '.log')

    if os.path.isfile(log_file):
        try:
            with open(log_file, 'r') as f:
                text = f.read()
            click.echo_via_pager(text)
        except OSError:
            click.echo(f'Could not open log file at \'{log_file}\'')
    else:
        click.echo_via_pager('')


@log.command(name='clear', help_priority=1)
@with_config_opt
def log_clear(config_name: str):
    """Clears Maestral's log file."""
    from maestral.utils.appdirs import get_log_path

    log_dir = get_log_path('maestral')
    log_name = config_name + '.log'

    log_files = []

    for file_name in os.listdir(log_dir):
        if file_name.startswith(log_name):
            log_files.append(os.path.join(log_dir, file_name))

    try:
        for file in log_files:
            open(file, 'w').close()
        click.echo('Cleared Maestral\'s log.')
    except FileNotFoundError:
        click.echo('Cleared Maestral\'s log.')
    except OSError:
        click.echo(f'Could not clear log at \'{log_dir}\'. Please try to delete it manually')


@log.command(name='level', help_priority=2)
@click.argument('level_name', required=False, type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR']))
@with_config_opt
def log_level(config_name: str, level_name: str):
    """Gets or sets the log level."""

    from maestral.daemon import MaestralProxy

    try:
        with MaestralProxy(config_name) as m:
            if level_name:
                m.log_level = logging._nameToLevel[level_name]
                click.echo(f'Log level set to {level_name}.')
            else:
                level_name = logging.getLevelName(m.log_level)
                click.echo(f'Log level:  {level_name}')
    except Pyro5.errors.CommunicationError:
        from maestral.config.main import MaestralConfig
        conf = MaestralConfig(config_name)
        if level_name:
            conf.set('app', 'log_level', logging._nameToLevel[level_name])
            click.echo(f'Log level set to {level_name}.')
        else:
            level_name = logging.getLevelName(conf.get('app', 'log_level'))
            click.echo(f'Log level:  {level_name}')


# ========================================================================================
# Management of different configurations
# ========================================================================================

@config.command(name='list', help_priority=0)
def config_list():
    """Lists all Maestral configurations."""
    click.echo('Available Maestral configurations:')
    for c in list_configs():
        click.echo('  ' + c)


@config.command(name='add', help_priority=1)
@click.argument('name')
def config_add(name: str):
    """Sets up and activates a fresh Maestral configuration."""
    if name in list_configs():
        click.echo(f'Configuration \'{name}\' already exists.')
    else:
        from maestral.config import MaestralConfig
        MaestralConfig(name)  # create instance to force file creation
        click.echo(f'Created configuration \'{name}\'.')


@config.command(name='remove', help_priority=2)
@click.argument('name')
def config_remove(name: str):
    """Removes a Maestral configuration."""
    if name not in list_configs():
        click.echo(f'Configuration \'{name}\' could not be found.')
    else:
        from maestral.utils.appdirs import get_conf_path
        for file in os.listdir(get_conf_path('maestral')):
            if file.startswith(name):
                os.unlink(os.path.join(get_conf_path('maestral'), file))
        click.echo(f'Deleted configuration \'{name}\'.')


# ========================================================================================
# Notifications commands
# ========================================================================================

@notify.command(name='level', help_priority=0)
@click.argument('level_name', required=False, type=click.Choice(['NONE', 'ERROR', 'SYNCISSUE', 'FILECHANGE']))
@with_config_opt
def notify_level(config_name: str, level_name: str):
    """Gets or sets the level for desktop notifications."""
    from maestral.daemon import MaestralProxy
    from maestral.utils.notify import levelNameToNumber, levelNumberToName

    try:
        with MaestralProxy(config_name) as m:
            if level_name:
                m.notification_level = levelNameToNumber(level_name)
                click.echo(f'Notification level set to {level_name}.')
            else:
                level_name = levelNumberToName(m.notification_level)
                click.echo(f'Notification level: {level_name}.')
    except Pyro5.errors.CommunicationError:
        from maestral.config.main import MaestralConfig
        conf = MaestralConfig(config_name)
        if level_name:
            conf.set('app', 'notification_level', levelNameToNumber(level_name))
            click.echo(f'Notification level set to {level_name}.')
        else:
            level_name = levelNumberToName(conf.get('app', 'notification_level'))
            click.echo(f'Notification level: {level_name}.')


@notify.command(name='snooze', help_priority=1)
@click.argument('minutes', type=click.IntRange(min=0))
@with_config_opt
def notify_snooze(config_name: str, minutes: int):
    """Snoozes desktop notifications of file changes."""

    from maestral.daemon import MaestralProxy

    try:
        with MaestralProxy(config_name) as m:
            m.notification_snooze = minutes
    except Pyro5.errors.CommunicationError:
        click.echo('Maestral daemon is not running.')
    else:
        if minutes > 0:
            click.echo(f'Notifications snoozed for {minutes} min. '
                       'Set snooze to 0 to reset.')
        else:
            click.echo(f'Notifications enabled.')
