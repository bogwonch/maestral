from __future__ import annotations

import functools
import sys
from typing import Callable, cast, TypeVar, Any, TYPE_CHECKING

import click

from .core import ConfigName
from .output import warn
from .utils import get_term_width

if TYPE_CHECKING:
    from ..daemon import MaestralProxy
    from ..main import Maestral


F = TypeVar("F", bound=Callable[..., Any])


def convert_api_errors(func: Callable) -> Callable:
    """
    Decorator that catches a MaestralApiError and prints a formatted error message to
    stdout before exiting. Calls ``sys.exit(1)`` after printing the error to stdout.
    """

    from ..exceptions import MaestralApiError

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except MaestralApiError as exc:
            warn(f"{exc.title}. {exc.message}")
            sys.exit(1)

    return wrapper


def check_for_fatal_errors(m: MaestralProxy | Maestral) -> bool:
    """
    Checks the given Maestral instance for fatal errors such as revoked Dropbox access,
    deleted Dropbox folder etc. Prints a nice representation to the command line.

    :param m: Proxy to Maestral daemon or Maestral instance.
    :returns: True in case of fatal errors, False otherwise.
    """

    import textwrap

    maestral_err_list = m.fatal_errors

    if len(maestral_err_list) > 0:

        width = get_term_width()

        err = maestral_err_list[0]
        wrapped_msg = textwrap.fill(err.message, width=width)

        click.echo("")
        click.secho(err.title, fg="red")
        click.secho(wrapped_msg, fg="red")
        click.echo("")

        return True
    else:
        return False


config_option = click.option(
    "-c",
    "--config-name",
    default="maestral",
    type=ConfigName(existing=False),
    is_eager=True,
    expose_value=True,
    help="Run command with the given configuration.",
)
existing_config_option = click.option(
    "-c",
    "--config-name",
    default="maestral",
    type=ConfigName(),
    is_eager=True,
    expose_value=True,
    help="Run command with the given configuration.",
)


def inject_proxy(fallback: bool, existing_config: bool):
    def decorator(f: F) -> F:
        def wrapper(*args, **kwargs):

            from ..daemon import MaestralProxy, CommunicationError

            ctx = click.get_current_context()

            config_name = ctx.params.pop("config_name", "maestral")
            kwargs.pop("config_name", None)

            try:
                proxy = ctx.with_resource(MaestralProxy(config_name, fallback=fallback))
            except CommunicationError:
                click.echo("Maestral daemon is not running.")
                ctx.exit(0)

            return ctx.invoke(f, proxy, *args, **kwargs)

        if existing_config:
            f = existing_config_option(f)
        else:
            f = config_option(f)

        return functools.update_wrapper(cast(F, wrapper), f)

    return decorator
