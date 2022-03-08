"""
This module contains the Dropbox API client. It wraps calls to the Dropbox Python SDK
and handles exceptions, chunked uploads or downloads, etc.
"""

from __future__ import annotations

# system imports
import os
import time
import logging
import contextlib
import threading
from datetime import datetime, timezone
from typing import Callable, Any, Iterator, TypeVar, Union, BinaryIO, TYPE_CHECKING

# external imports
import requests
from dropbox import Dropbox, create_session, files, sharing, users, exceptions, common
from dropbox.common import PathRoot
from dropbox.oauth import DropboxOAuth2FlowNoRedirect

# local imports
from . import __version__
from .keyring import CredentialStorage, TokenType
from .exceptions import (
    MaestralApiError,
    SyncError,
    PathError,
    NotFoundError,
    NotLinkedError,
    DataCorruptionError,
)
from .errorhandling import (
    convert_api_errors,
    dropbox_to_maestral_error,
    retry_on_error,
    CONNECTION_ERRORS,
)
from .config import MaestralState
from .constants import DROPBOX_APP_KEY
from .utils import natural_size, chunks, clamp
from .utils.path import opener_no_symlink, delete
from .utils.hashing import DropboxContentHasher, StreamHasher

if TYPE_CHECKING:
    from .models import SyncEvent


__all__ = ["DropboxClient"]


PaginationResultType = Union[sharing.ListSharedLinksResult, files.ListFolderResult]
FT = TypeVar("FT", bound=Callable[..., Any])

_major_minor_version = ".".join(__version__.split(".")[:2])
USER_AGENT = f"Maestral/v{_major_minor_version}"


def get_hash(data: bytes) -> str:
    hasher = DropboxContentHasher()
    hasher.update(data)
    return hasher.hexdigest()


class DropboxClient:
    """Client for the Dropbox SDK

    This client defines basic methods to wrap Dropbox Python SDK calls, such as
    creating, moving, modifying and deleting files and folders on Dropbox and
    downloading files from Dropbox.

    All Dropbox SDK exceptions, OSErrors from the local file system API and connection
    errors will be caught and reraised as a subclass of
    :exc:`maestral.exceptions.MaestralApiError`.

    This class can be used as a context manager to clean up any network resources from
    the API requests.

    :Example:

        >>> from maestral.client import DropboxClient
        >>> with DropboxClient("maestral") as client:
        ...     res = client.list_folder("/")
        >>> print(res.entries)

    :param config_name: Name of config file and state file to use.
    :param timeout: Timeout for individual requests. Defaults to 100 sec if not given.
    :param session: Optional requests session to use. If not given, a new session will
        be created with :func:`dropbox.dropbox_client.create_session`.
    """

    SDK_VERSION: str = "2.0"

    MAX_TRANSFER_RETRIES = 3
    MAX_LIST_FOLDER_RETRIES = 3

    _dbx: Dropbox | None

    def __init__(
        self,
        config_name: str,
        timeout: float = 100,
        session: requests.Session | None = None,
    ) -> None:

        self.config_name = config_name
        self._auth_flow: DropboxOAuth2FlowNoRedirect | None = None
        self.cred_storage = CredentialStorage(config_name)

        self._state = MaestralState(config_name)
        self._logger = logging.getLogger(__name__)

        self._timeout = timeout
        self._session = session or create_session()
        self._backoff_until = 0
        self._dbx: Dropbox | None = None
        self._dbx_base: Dropbox | None = None
        self._cached_account_info: users.FullAccount | None = None
        self._namespace_id = self._state.get("account", "path_root_nsid")
        self._is_team_space = self._state.get("account", "path_root_type") == "team"
        self._lock = threading.Lock()

    # ---- Linking API -----------------------------------------------------------------

    @property
    def dbx_base(self) -> Dropbox:
        """The underlying Dropbox SDK instance without namespace headers."""

        if not self._dbx_base:
            self._init_sdk()

        return self._dbx_base

    @property
    def dbx(self) -> Dropbox:
        """The underlying Dropbox SDK instance with namespace headers."""

        if not self._dbx:
            self._init_sdk()

        return self._dbx

    @property
    def linked(self) -> bool:
        """
        Indicates if the client is linked to a Dropbox account (read only). This will
        block until the user's keyring is unlocked to load the saved auth token.

        :raises KeyringAccessError: if keyring access fails.
        """
        return self.cred_storage.token is not None

    def get_auth_url(self) -> str:
        """
        Returns a URL to authorize access to a Dropbox account. To link a Dropbox
        account, retrieve an auth token from the URL and link Maestral by calling
        :meth:`link` with the provided token.

        :returns: URL to retrieve an OAuth token.
        """
        self._auth_flow = DropboxOAuth2FlowNoRedirect(
            consumer_key=DROPBOX_APP_KEY,
            token_access_type="offline",
            use_pkce=True,
        )
        return self._auth_flow.start()

    def link(self, token: str) -> int:
        """
        Links Maestral with a Dropbox account using the given access token. The token
        will be stored for future usage as documented in the :mod:`oauth` module.

        :param token: OAuth token for Dropbox access.
        :returns: 0 on success, 1 for an invalid token and 2 for connection errors.
        """

        if not self._auth_flow:
            raise RuntimeError("Please start auth flow with 'get_auth_url' first")

        try:
            res = self._auth_flow.finish(token)
        except requests.exceptions.HTTPError:
            return 1
        except CONNECTION_ERRORS:
            return 2

        self._init_sdk(res.refresh_token, TokenType.Offline)

        try:
            self.update_path_root()
        except CONNECTION_ERRORS:
            return 2

        self.cred_storage.save_creds(
            res.account_id, res.refresh_token, TokenType.Offline
        )
        self._auth_flow = None

        return 0

    def unlink(self) -> None:
        """
        Unlinks the Dropbox account.

        :raises KeyringAccessError: if keyring access fails.
        :raises DropboxAuthError: if we cannot authenticate with Dropbox.
        """

        self._dbx = None
        self._dbx_base = None
        self._cached_account_info = None

        with convert_api_errors():
            self.dbx_base.auth_token_revoke()
            self.cred_storage.delete_creds()

    def _init_sdk(
        self, token: str | None = None, token_type: TokenType | None = None
    ) -> None:
        """
        Initialise the SDK. If no token is given, get the token from our credential
        storage.

        :param token: Token for the SDK.
        :param token_type: Token type
        :raises RuntimeError: if token is not available from storage and no token is
            passed as an argument.
        """

        with self._lock:

            if self._dbx:
                return

            if not (token or self.cred_storage.token):
                raise NotLinkedError(
                    "No auth token set", "Please link a Dropbox account first."
                )

            token = token or self.cred_storage.token
            token_type = token_type or self.cred_storage.token_type

            if token_type is TokenType.Offline:

                # Initialise Dropbox SDK.
                self._dbx_base = Dropbox(
                    oauth2_refresh_token=token,
                    app_key=DROPBOX_APP_KEY,
                    session=self._session,
                    user_agent=USER_AGENT,
                    timeout=self._timeout,
                )
            else:
                # Initialise Dropbox SDK.
                self._dbx_base = Dropbox(
                    oauth2_access_token=token,
                    app_key=DROPBOX_APP_KEY,
                    session=self._session,
                    user_agent=USER_AGENT,
                    timeout=self._timeout,
                )

            # If namespace_id was given, use the corresponding namespace, otherwise
            # default to the home namespace.
            if self._namespace_id:
                root_path = PathRoot.root(self._namespace_id)
                self._dbx = self._dbx_base.with_path_root(root_path)
            else:
                self._dbx = self._dbx_base

    @property
    def account_info(self) -> users.FullAccount:
        """Returns cached account info. Use :meth:`get_account_info` to get the latest
        account info from Dropbox servers."""

        if not self._cached_account_info:
            return self.get_account_info()
        else:
            return self._cached_account_info

    @property
    def namespace_id(self) -> str:
        """The namespace ID of the path root currently used by the DropboxClient. All
        file paths will be interpreted as relative to the root namespace. Use
        :meth:`update_path_root` to update the root namespace after the user joins or
        leaves a team with a Team Space."""
        return self._namespace_id

    @property
    def is_team_space(self) -> bool:
        """Whether the user's Dropbox uses a Team Space. Use :meth:`update_path_root` to
        update the root namespace after the user joins or eaves a team with a Team
        Space."""
        return self._is_team_space

    # ---- Session management ----------------------------------------------------------

    def close(self) -> None:
        """Cleans up all resources like the request session/network connection."""
        if self._dbx:
            self._dbx.close()

    def __enter__(self) -> DropboxClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def clone(
        self,
        config_name: str | None = None,
        timeout: float | None = None,
        session: requests.Session | None = None,
    ) -> DropboxClient:
        """
        Creates a new copy of the Dropbox client with the same defaults unless modified
        by arguments to :meth:`clone`.

        :param config_name: Name of config file and state file to use.
        :param timeout: Timeout for individual requests.
        :param session: Requests session to use.
        :returns: A new instance of DropboxClient.
        """

        config_name = config_name or self.config_name
        timeout = timeout or self._timeout
        session = session or self._session

        client = self.__class__(config_name, timeout, session)

        if self._dbx:
            client._dbx = self._dbx.clone(session=session)

        if self._dbx_base:
            client._dbx_base = self._dbx_base.clone(session=session)

        return client

    def clone_with_new_session(self) -> DropboxClient:
        """
        Creates a new copy of the Dropbox client with the same defaults but a new
        requests session.

        :returns: A new instance of DropboxClient.
        """
        return self.clone(session=create_session())

    def update_path_root(self, root_info: common.RootInfo | None = None) -> None:
        """
        Updates the root path for the Dropbox client. All files paths given as arguments
        to API calls such as :meth:`list_folder` or :meth:`get_metadata` will be
        interpreted as relative to the root path. All file paths returned by API calls,
        for instance in file metadata, will be relative to this root path.

        The root namespace will change when the user joins or leaves a Dropbox Team with
        Team Spaces. If this happens, API calls using the old root namespace will raise
        a :exc:`maestral.exceptions.PathRootError`. Use this method to update to the new
        root namespace.

        See https://developers.dropbox.com/dbx-team-files-guide and
        https://www.dropbox.com/developers/reference/path-root-header-modes for more
        information on Dropbox Team namespaces and path root headers in API calls.

        .. note:: We don't automatically switch root namespaces because API users may
            want to take action when the path root has changed before making further API
            calls. Be prepared to handle :exc:`maestral.exceptions.PathRootError`
            and act accordingly for all methods.

        :param root_info: Optional :class:`dropbox.common.RootInfo` describing the path
            root. If not given, the latest root info will be fetched from Dropbox
            servers.
        """

        with convert_api_errors():

            if not root_info:
                account_info = self.get_account_info()
                root_info = account_info.root_info

            root_nsid = root_info.root_namespace_id

            path_root = PathRoot.root(root_nsid)
            self._dbx = self.dbx_base.with_path_root(path_root)

        if isinstance(root_info, common.UserRootInfo):
            actual_root_type = "user"
            actual_home_path = ""
        elif isinstance(root_info, common.TeamRootInfo):
            actual_root_type = "team"
            actual_home_path = root_info.home_path
        else:
            raise MaestralApiError(
                "Unknown root namespace type",
                f"Got {root_info!r} but expected UserRootInfo or TeamRootInfo.",
            )

        self._namespace_id = root_nsid
        self._is_team_space = actual_root_type == "team"

        self._state.set("account", "path_root_nsid", root_nsid)
        self._state.set("account", "path_root_type", actual_root_type)
        self._state.set("account", "home_path", actual_home_path)

        self._logger.debug("Path root type: %s", actual_root_type)
        self._logger.debug("Path root nsid: %s", root_info.root_namespace_id)
        self._logger.debug("User home path: %s", actual_home_path)

    # ---- SDK wrappers ----------------------------------------------------------------

    def get_account_info(self, dbid: str | None = None) -> users.FullAccount:
        """
        Gets current account information.

        :param dbid: Dropbox ID of account. If not given, will get the info of the
            currently linked account.
        :returns: Account info.
        """

        with convert_api_errors():
            if dbid:
                res = self.dbx_base.users_get_account(dbid)
            else:
                res = self.dbx_base.users_get_current_account()

        if not dbid:
            # Save our own account info to config.
            if res.account_type.is_basic():
                account_type = "basic"
            elif res.account_type.is_business():
                account_type = "business"
            elif res.account_type.is_pro():
                account_type = "pro"
            else:
                account_type = ""

            self._state.set("account", "email", res.email)
            self._state.set("account", "display_name", res.name.display_name)
            self._state.set("account", "abbreviated_name", res.name.abbreviated_name)
            self._state.set("account", "type", account_type)

        if not self._namespace_id:
            home_nsid = res.root_info.home_namespace_id
            self._namespace_id = home_nsid
            self._state.set("account", "path_root_nsid", home_nsid)

        self._cached_account_info = res

        return res

    def get_space_usage(self) -> users.SpaceUsage:
        """
        :returns: The space usage of the currently linked account.
        """
        with convert_api_errors():
            res = self.dbx_base.users_get_space_usage()

        # Query space usage type.
        if res.allocation.is_team():
            usage_type = "team"
        elif res.allocation.is_individual():
            usage_type = "individual"
        else:
            usage_type = ""

        # Generate space usage string.
        if res.allocation.is_team():
            used = res.allocation.get_team().used
            allocated = res.allocation.get_team().allocated
        else:
            used = res.used
            allocated = res.allocation.get_individual().allocated

        percent = used / allocated
        space_usage = f"{percent:.1%} of {natural_size(allocated)} used"

        # Save results to config.
        self._state.set("account", "usage", space_usage)
        self._state.set("account", "usage_type", usage_type)

        return res

    def get_metadata(
        self, dbx_path: str, include_deleted: bool = False
    ) -> files.Metadata | None:
        """
        Gets metadata for an item on Dropbox or returns ``False`` if no metadata is
        available. Keyword arguments are passed on to Dropbox SDK files_get_metadata
        call.

        :param dbx_path: Path of folder on Dropbox.
        :param include_deleted: Whether to return data for deleted items.
        :returns: Metadata of item at the given path or ``None`` if item cannot be found.
        """

        try:
            with convert_api_errors(dbx_path=dbx_path):
                return self.dbx.files_get_metadata(
                    dbx_path, include_deleted=include_deleted
                )
        except (NotFoundError, PathError):
            return None

    def list_revisions(
        self, dbx_path: str, mode: str = "path", limit: int = 10
    ) -> files.ListRevisionsResult:
        """
        Lists all file revisions for the given file.

        :param dbx_path: Path to file on Dropbox.
        :param mode: Must be 'path' or 'id'. If 'id', specify the Dropbox file ID
            instead of the file path to get revisions across move and rename events.
        :param limit: Maximum number of revisions to list.
        :returns: File revision history.
        """

        with convert_api_errors(dbx_path=dbx_path):
            mode = files.ListRevisionsMode(mode)
            return self.dbx.files_list_revisions(dbx_path, mode=mode, limit=limit)

    def restore(self, dbx_path: str, rev: str) -> files.FileMetadata:
        """
        Restore an old revision of a file.

        :param dbx_path: The path to save the restored file.
        :param rev: The revision to restore. Old revisions can be listed with
            :meth:`list_revisions`.
        :returns: Metadata of restored file.
        """

        with convert_api_errors(dbx_path=dbx_path):
            return self.dbx.files_restore(dbx_path, rev)

    @retry_on_error(DataCorruptionError, MAX_TRANSFER_RETRIES)
    def download(
        self,
        dbx_path: str,
        local_path: str,
        sync_event: SyncEvent | None = None,
    ) -> files.FileMetadata:
        """
        Downloads a file from Dropbox to given local path.

        :param dbx_path: Path to file on Dropbox or rev number.
        :param local_path: Path to local download destination.
        :param sync_event: If given, the sync event will be updated with the number of
            downloaded bytes.
        :returns: Metadata of downloaded item.
        :raises DataCorruptionError: if data is corrupted during download.
        """

        with convert_api_errors(dbx_path=dbx_path):

            md = self.dbx.files_get_metadata(dbx_path)

            if isinstance(md, files.FileMetadata) and md.symlink_info:
                # Don't download but reproduce symlink locally.
                try:
                    os.unlink(local_path)
                except FileNotFoundError:
                    pass
                os.symlink(md.symlink_info.target, local_path)

            else:
                chunk_size = 2**13

                md, http_resp = self.dbx.files_download(dbx_path)

                hasher = DropboxContentHasher()

                with open(local_path, "wb", opener=opener_no_symlink) as f:

                    wrapped_f = StreamHasher(f, hasher)

                    with contextlib.closing(http_resp):
                        for c in http_resp.iter_content(chunk_size):
                            wrapped_f.write(c)
                            if sync_event:
                                sync_event.completed = wrapped_f.tell()

                    local_hash = hasher.hexdigest()

                    if md.content_hash != local_hash:
                        delete(local_path)
                        raise DataCorruptionError(
                            "Data corrupted", "Please retry download."
                        )

            # Dropbox SDK provides naive datetime in UTC.
            client_mod = md.client_modified.replace(tzinfo=timezone.utc)
            server_mod = md.server_modified.replace(tzinfo=timezone.utc)

            # Enforce client_modified < server_modified.
            timestamp = min(client_mod.timestamp(), server_mod.timestamp(), time.time())
            # Set mtime of downloaded file.
            os.utime(local_path, (time.time(), timestamp), follow_symlinks=False)

        return md

    def upload(
        self,
        local_path: str,
        dbx_path: str,
        chunk_size: int = 5 * 10**6,
        mode: files.WriteMode = files.WriteMode.add,
        autorename: bool = False,
        sync_event: SyncEvent | None = None,
    ) -> files.FileMetadata:
        """
        Uploads local file to Dropbox.

        :param local_path: Path of local file to upload.
        :param dbx_path: Path to save file on Dropbox.
        :param chunk_size: Maximum size for individual uploads. If larger than 150 MB,
            it will be set to 150 MB.
        :param mode: Your intent when writing a file to some path. This is used to
            determine what constitutes a conflict and what the autorename strategy is.
            See :class:`dropbox.files.WriteMode` for exact behavior.
        :param sync_event: If given, the sync event will be updated with the number of
            downloaded bytes.
        :param autorename: If there's a conflict, as determined by ``mode``, have the
            Dropbox server try to autorename the file to avoid conflict. The default for
            this field is False.
        :returns: Metadata of uploaded file.
        :raises DataCorruptionError: if data is corrupted during upload.
        """

        chunk_size = clamp(chunk_size, 10**5, 150 * 10**6)

        with convert_api_errors(dbx_path=dbx_path, local_path=local_path):

            stat = os.lstat(local_path)

            # Dropbox SDK takes naive datetime in UTC
            mtime_dt = datetime.utcfromtimestamp(stat.st_mtime)

            if stat.st_size <= chunk_size:

                # Upload all at once.

                return self._upload_helper(
                    local_path, dbx_path, mtime_dt, mode, autorename, sync_event
                )

            else:

                # Upload in chunks.
                # Note: We currently do not support resuming interrupted uploads.
                # Dropbox keeps upload sessions open for 48h so this could be done in
                # the future.

                with open(local_path, "rb", opener=opener_no_symlink) as f:

                    session_id = self._upload_session_start_helper(
                        f, chunk_size, dbx_path, sync_event
                    )

                    while stat.st_size - f.tell() > chunk_size:
                        self._upload_session_append_helper(
                            f, session_id, chunk_size, dbx_path, sync_event
                        )

                    return self._upload_session_finish_helper(
                        f,
                        session_id,
                        chunk_size,
                        # Commit info.
                        dbx_path,
                        mtime_dt,
                        mode,
                        autorename,
                        # Commit info end.
                        sync_event,
                    )

    @retry_on_error(DataCorruptionError, MAX_TRANSFER_RETRIES)
    def _upload_helper(
        self,
        local_path: str,
        dbx_path: str,
        client_modified: datetime,
        mode: files.WriteMode,
        autorename: bool,
        sync_event: SyncEvent | None,
    ) -> files.FileMetadata:

        with open(local_path, "rb", opener=opener_no_symlink) as f:
            data = f.read()

            with convert_api_errors(dbx_path=dbx_path, local_path=local_path):
                md = self.dbx.files_upload(
                    data,
                    dbx_path,
                    client_modified=client_modified,
                    content_hash=get_hash(data),
                    mode=mode,
                    autorename=autorename,
                )

            if sync_event:
                sync_event.completed = f.tell()

        return md

    @retry_on_error(DataCorruptionError, MAX_TRANSFER_RETRIES)
    def _upload_session_start_helper(
        self,
        f: BinaryIO,
        chunk_size: int,
        dbx_path: str,
        sync_event: SyncEvent | None,
    ) -> str:

        initial_offset = f.tell()
        data = f.read(chunk_size)

        try:
            with convert_api_errors(dbx_path=dbx_path):
                session_start = self.dbx.files_upload_session_start(
                    data, content_hash=get_hash(data)
                )
        except Exception:
            # Return to previous position in file.
            f.seek(initial_offset)
            raise

        if sync_event:
            sync_event.completed = f.tell()

        return session_start.session_id

    @retry_on_error(DataCorruptionError, MAX_TRANSFER_RETRIES)
    def _upload_session_append_helper(
        self,
        f: BinaryIO,
        session_id: str,
        chunk_size: int,
        dbx_path: str,
        sync_event: SyncEvent | None,
    ) -> None:

        initial_offset = f.tell()
        data = f.read(chunk_size)

        cursor = files.UploadSessionCursor(
            session_id=session_id,
            offset=initial_offset,
        )

        try:
            with convert_api_errors(dbx_path=dbx_path):
                self.dbx.files_upload_session_append_v2(
                    data, cursor, content_hash=get_hash(data)
                )
        except exceptions.DropboxException as exc:
            error = getattr(exc, "error", None)
            if (
                isinstance(error, files.UploadSessionAppendError)
                and error.is_incorrect_offset()
            ):
                offset_error = error.get_incorrect_offset()
                last_successful_offset = offset_error.correct_offset
                f.seek(last_successful_offset)
            raise exc

        except Exception:
            f.seek(initial_offset)
            raise

        if sync_event:
            sync_event.completed = f.tell()

    @retry_on_error(DataCorruptionError, MAX_TRANSFER_RETRIES)
    def _upload_session_finish_helper(
        self,
        f: BinaryIO,
        session_id: str,
        chunk_size: int,
        dbx_path: str,
        client_modified: datetime,
        mode: files.WriteMode,
        autorename: bool,
        sync_event: SyncEvent | None,
    ) -> files.FileMetadata:

        initial_offset = f.tell()
        data = f.read(chunk_size)

        if len(data) > chunk_size:
            raise RuntimeError("Too much data left to finish the session")

        # Finish upload session and return metadata.

        cursor = files.UploadSessionCursor(
            session_id=session_id,
            offset=initial_offset,
        )
        commit = files.CommitInfo(
            path=dbx_path,
            client_modified=client_modified,
            autorename=autorename,
            mode=mode,
        )

        try:
            with convert_api_errors(dbx_path=dbx_path):
                md = self.dbx.files_upload_session_finish(
                    data, cursor, commit, content_hash=get_hash(data)
                )
        except exceptions.DropboxException as exc:
            error = getattr(exc, "error", None)
            if (
                isinstance(error, files.UploadSessionFinishError)
                and error.is_lookup_failed()
                and error.get_lookup_failed().is_incorrect_offset()
            ):
                offset_error = error.get_lookup_failed().get_incorrect_offset()
                last_successful_offset = offset_error.correct_offset
                f.seek(last_successful_offset)
            raise exc

        except Exception:
            # Return to previous position in file.
            f.seek(initial_offset)
            raise

        if sync_event:
            sync_event.completed = sync_event.size

        return md

    def remove(self, dbx_path: str, parent_rev: str | None = None) -> files.Metadata:
        """
        Removes a file / folder from Dropbox.

        :param dbx_path: Path to file on Dropbox.
        :param parent_rev: Perform delete if given "rev" matches the existing file's
            latest "rev". This field does not support deleting a folder.
        :returns: Metadata of deleted item.
        """

        with convert_api_errors(dbx_path=dbx_path):
            res = self.dbx.files_delete_v2(dbx_path, parent_rev=parent_rev)
            return res.metadata

    def remove_batch(
        self, entries: list[tuple[str, str]], batch_size: int = 900
    ) -> list[files.Metadata | MaestralApiError]:
        """
        Deletes multiple items on Dropbox in a batch job.

        :param entries: List of Dropbox paths and "rev"s to delete. If a "rev" is not
            None, the file will only be deleted if it matches the rev on Dropbox. This
            is not supported when deleting a folder.
        :param batch_size: Number of items to delete in each batch. Dropbox allows
            batches of up to 1,000 items. Larger values will be capped automatically.
        :returns: List of Metadata for deleted items or SyncErrors for failures. Results
            will be in the same order as the original input.
        """

        batch_size = clamp(batch_size, 1, 1000)

        res_entries = []
        result_list = []

        # Up two ~ 1,000 entries allowed per batch:
        # https://www.dropbox.com/developers/reference/data-ingress-guide
        for chunk in chunks(entries, n=batch_size):

            arg = [files.DeleteArg(e[0], e[1]) for e in chunk]

            with convert_api_errors():
                res = self.dbx.files_delete_batch(arg)

            if res.is_complete():
                batch_res = res.get_complete()
                res_entries.extend(batch_res.entries)

            elif res.is_async_job_id():
                async_job_id = res.get_async_job_id()

                time.sleep(0.5)

                with convert_api_errors():
                    res = self.dbx.files_delete_batch_check(async_job_id)

                check_interval = round(len(chunk) / 100, 1)

                while res.is_in_progress():
                    time.sleep(check_interval)
                    with convert_api_errors():
                        res = self.dbx.files_delete_batch_check(async_job_id)

                if res.is_complete():
                    batch_res = res.get_complete()
                    res_entries.extend(batch_res.entries)

                elif res.is_failed():
                    error = res.get_failed()
                    if error.is_too_many_write_operations():
                        title = "Could not delete items"
                        text = (
                            "There are too many write operations happening in your "
                            "Dropbox. Please try again later."
                        )
                        raise SyncError(title, text)

        for i, entry in enumerate(res_entries):
            if entry.is_success():
                result_list.append(entry.get_success().metadata)
            elif entry.is_failure():
                exc = exceptions.ApiError(
                    error=entry.get_failure(),
                    user_message_text="",
                    user_message_locale="",
                    request_id="",
                )
                sync_err = dropbox_to_maestral_error(exc, dbx_path=entries[i][0])
                result_list.append(sync_err)

        return result_list

    def move(
        self, dbx_path: str, new_path: str, autorename: bool = False
    ) -> files.Metadata:
        """
        Moves / renames files or folders on Dropbox.

        :param dbx_path: Path to file/folder on Dropbox.
        :param new_path: New path on Dropbox to move to.
        :param autorename: Have the Dropbox server try to rename the item in case of a
            conflict.
        :returns: Metadata of moved item.
        """

        with convert_api_errors(dbx_path=new_path):
            res = self.dbx.files_move_v2(
                dbx_path,
                new_path,
                allow_shared_folder=True,
                allow_ownership_transfer=True,
                autorename=autorename,
            )
            return res.metadata

    def make_dir(self, dbx_path: str, autorename: bool = False) -> files.FolderMetadata:
        """
        Creates a folder on Dropbox.

        :param dbx_path: Path of Dropbox folder.
        :param autorename: Have the Dropbox server try to rename the item in case of a
            conflict.
        :returns: Metadata of created folder.
        """

        with convert_api_errors(dbx_path=dbx_path):
            res = self.dbx.files_create_folder_v2(dbx_path, autorename)
            return res.metadata

    def make_dir_batch(
        self,
        dbx_paths: list[str],
        batch_size: int = 900,
        autorename: bool = False,
        force_async: bool = False,
    ) -> list[files.Metadata | MaestralApiError]:
        """
        Creates multiple folders on Dropbox in a batch job.

        :param dbx_paths: List of dropbox folder paths.
        :param batch_size: Number of folders to create in each batch. Dropbox allows
            batches of up to 1,000 folders. Larger values will be capped automatically.
        :param autorename: Have the Dropbox server try to rename the item in case of a
            conflict.
        :param force_async: Whether to force asynchronous creation on Dropbox servers.
        :returns: List of Metadata for created folders or SyncError for failures.
            Entries will be in the same order as given paths.
        """
        batch_size = clamp(batch_size, 1, 1000)

        entries = []
        result_list = []

        with convert_api_errors():

            # Up two ~ 1,000 entries allowed per batch:
            # https://www.dropbox.com/developers/reference/data-ingress-guide
            for chunk in chunks(dbx_paths, n=batch_size):
                res = self.dbx.files_create_folder_batch(chunk, autorename, force_async)
                if res.is_complete():
                    batch_res = res.get_complete()
                    entries.extend(batch_res.entries)
                elif res.is_async_job_id():
                    async_job_id = res.get_async_job_id()

                    time.sleep(0.5)
                    res = self.dbx.files_create_folder_batch_check(async_job_id)

                    check_interval = round(len(chunk) / 100, 1)

                    while res.is_in_progress():
                        time.sleep(check_interval)
                        res = self.dbx.files_create_folder_batch_check(async_job_id)

                    if res.is_complete():
                        batch_res = res.get_complete()
                        entries.extend(batch_res.entries)

                    elif res.is_failed():
                        error = res.get_failed()
                        if error.is_too_many_files():
                            res_list = self.make_dir_batch(
                                chunk, round(batch_size / 2), autorename, force_async
                            )
                            result_list.extend(res_list)

        for i, entry in enumerate(entries):
            if entry.is_success():
                result_list.append(entry.get_success().metadata)
            elif entry.is_failure():
                exc = exceptions.ApiError(
                    error=entry.get_failure(),
                    user_message_text="",
                    user_message_locale="",
                    request_id="",
                )
                sync_err = dropbox_to_maestral_error(exc, dbx_path=dbx_paths[i])
                result_list.append(sync_err)

        return result_list

    def share_dir(self, dbx_path: str, **kwargs) -> sharing.SharedFolderMetadata:
        """
        Converts a Dropbox folder to a shared folder. Creates the folder if it does not
        exist.

        :param dbx_path: Path of Dropbox folder.
        :param kwargs: Keyword arguments for the Dropbox API files_create_folder_v2
            endpoint.
        :returns: Metadata of shared folder.
        """

        dbx_path = "" if dbx_path == "/" else dbx_path

        with convert_api_errors(dbx_path=dbx_path):
            res = self.dbx.sharing_share_folder(dbx_path, **kwargs)

        if res.is_complete():
            return res.get_complete()

        elif res.is_async_job_id():
            async_job_id = res.get_async_job_id()

            time.sleep(0.2)

            with convert_api_errors(dbx_path=dbx_path):
                job_status = self.dbx.sharing_check_share_job_status(async_job_id)

            while job_status.is_in_progress():
                time.sleep(0.2)

                with convert_api_errors(dbx_path=dbx_path):
                    job_status = self.dbx.sharing_check_share_job_status(async_job_id)

            if job_status.is_complete():
                return job_status.get_complete()

            elif job_status.is_failed():
                error = job_status.get_failed()
                exc = exceptions.ApiError(
                    error=error,
                    user_message_locale="",
                    user_message_text="",
                    request_id="",
                )
                raise dropbox_to_maestral_error(exc)

    def get_latest_cursor(
        self, dbx_path: str, include_non_downloadable_files: bool = False, **kwargs
    ) -> str:
        """
        Gets the latest cursor for the given folder and subfolders.

        :param dbx_path: Path of folder on Dropbox.
        :param include_non_downloadable_files: If ``True``, files that cannot be
            downloaded (at the moment only G-suite files on Dropbox) will be included.
        :param kwargs: Additional keyword arguments for Dropbox API
            files/list_folder/get_latest_cursor endpoint.
        :returns: The latest cursor representing a state of a folder and its subfolders.
        """

        dbx_path = "" if dbx_path == "/" else dbx_path

        with convert_api_errors(dbx_path=dbx_path):
            res = self.dbx.files_list_folder_get_latest_cursor(
                dbx_path,
                include_non_downloadable_files=include_non_downloadable_files,
                recursive=True,
                **kwargs,
            )

        return res.cursor

    def list_folder(
        self,
        dbx_path: str,
        recursive: bool = False,
        include_deleted: bool = False,
        include_mounted_folders: bool = True,
        include_non_downloadable_files: bool = False,
    ) -> files.ListFolderResult:
        """
        Lists the contents of a folder on Dropbox. Similar to
        :meth:`list_folder_iterator` but returns all entries in a single
        :class:`dropbox.files.ListFolderResult` instance.

        :param dbx_path: Path of folder on Dropbox.
        :param dbx_path: Path of folder on Dropbox.
        :param recursive: If true, the list folder operation will be applied recursively
            to all subfolders and the response will contain contents of all subfolders.
        :param include_deleted: If true, the results will include entries for files and
            folders that used to exist but were deleted.
        :param bool include_mounted_folders: If true, the results will include
            entries under mounted folders which includes app folder, shared
            folder and team folder.
        :param bool include_non_downloadable_files: If true, include files that
            are not downloadable, i.e. Google Docs.
        :returns: Content of given folder.
        """

        iterator = self.list_folder_iterator(
            dbx_path,
            recursive=recursive,
            include_deleted=include_deleted,
            include_mounted_folders=include_mounted_folders,
            include_non_downloadable_files=include_non_downloadable_files,
        )

        return self.flatten_results(list(iterator), attribute_name="entries")

    def list_folder_iterator(
        self,
        dbx_path: str,
        recursive: bool = False,
        include_deleted: bool = False,
        include_mounted_folders: bool = True,
        limit: int | None = None,
        include_non_downloadable_files: bool = False,
    ) -> Iterator[files.ListFolderResult]:
        """
        Lists the contents of a folder on Dropbox. Returns an iterator yielding
        :class:`dropbox.files.ListFolderResult` instances. The number of entries
        returned in each iteration corresponds to the number of entries returned by a
        single Dropbox API call and will be typically around 500.

        :param dbx_path: Path of folder on Dropbox.
        :param recursive: If true, the list folder operation will be applied recursively
            to all subfolders and the response will contain contents of all subfolders.
        :param include_deleted: If true, the results will include entries for files and
            folders that used to exist but were deleted.
        :param bool include_mounted_folders: If true, the results will include
            entries under mounted folders which includes app folder, shared
            folder and team folder.
        :param Nullable[int] limit: The maximum number of results to return per
            request. Note: This is an approximate number and there can be
            slightly more entries returned in some cases.
        :param bool include_non_downloadable_files: If true, include files that
            are not downloadable, i.e. Google Docs.
        :returns: Iterator over content of given folder.
        """

        with convert_api_errors(dbx_path):

            dbx_path = "" if dbx_path == "/" else dbx_path

            res = self.dbx.files_list_folder(
                dbx_path,
                recursive=recursive,
                include_deleted=include_deleted,
                include_mounted_folders=include_mounted_folders,
                limit=limit,
                include_non_downloadable_files=include_non_downloadable_files,
            )

            yield res

            while res.has_more:
                res = self._list_folder_continue_helper(res.cursor)
                yield res

    @retry_on_error(requests.exceptions.ReadTimeout, MAX_LIST_FOLDER_RETRIES, backoff=3)
    def _list_folder_continue_helper(self, cursor: str) -> files.ListFolderResult:
        return self.dbx.files_list_folder_continue(cursor)

    def wait_for_remote_changes(self, last_cursor: str, timeout: int = 40) -> bool:
        """
        Waits for remote changes since ``last_cursor``. Call this method after
        starting the Dropbox client and periodically to get the latest updates.

        :param last_cursor: Last to cursor to compare for changes.
        :param timeout: Seconds to wait until timeout. Must be between 30 and 480. The
            Dropbox API will add a random jitter of up to 60 sec to this value.
        :returns: ``True`` if changes are available, ``False`` otherwise.
        """

        if not 30 <= timeout <= 480:
            raise ValueError("Timeout must be in range [30, 480]")

        # Honour last request to back off.
        time_to_backoff = max(self._backoff_until - time.time(), 0)
        time.sleep(time_to_backoff)

        with convert_api_errors():
            res = self.dbx.files_list_folder_longpoll(last_cursor, timeout=timeout)

        # Keep track of last longpoll, back off if requested by API.
        if res.backoff:
            self._logger.debug("Backoff requested for %s sec", res.backoff)
            self._backoff_until = time.time() + res.backoff + 5.0
        else:
            self._backoff_until = 0

        return res.changes

    def list_remote_changes(self, last_cursor: str) -> files.ListFolderResult:
        """
        Lists changes to remote Dropbox since ``last_cursor``. Same as
        :meth:`list_remote_changes_iterator` but fetches all changes first and returns
        a single :class:`dropbox.files.ListFolderResult`. This may be useful if you want
        to fetch all changes in advance before starting to process them.

        :param last_cursor: Last to cursor to compare for changes.
        :returns: Remote changes since given cursor.
        """

        iterator = self.list_remote_changes_iterator(last_cursor)
        return self.flatten_results(list(iterator), attribute_name="entries")

    def list_remote_changes_iterator(
        self, last_cursor: str
    ) -> Iterator[files.ListFolderResult]:
        """
        Lists changes to the remote Dropbox since ``last_cursor``. Returns an iterator
        yielding :class:`dropbox.files.ListFolderResult` instances. The number of
        entries returned in each iteration corresponds to the number of entries returned
        by a single Dropbox API call and will be typically around 500.

        Call this after :meth:`wait_for_remote_changes` returns ``True``.

        :param last_cursor: Last to cursor to compare for changes.
        :returns: Iterator over remote changes since given cursor.
        """

        with convert_api_errors():

            result = self.dbx.files_list_folder_continue(last_cursor)

            yield result

            while result.has_more:
                result = self.dbx.files_list_folder_continue(result.cursor)
                yield result

    def create_shared_link(
        self,
        dbx_path: str,
        visibility: sharing.RequestedVisibility = sharing.RequestedVisibility.public,
        password: str | None = None,
        expires: datetime | None = None,
        **kwargs,
    ) -> sharing.SharedLinkMetadata:
        """
        Creates a shared link for the given path. Some options are only available for
        Professional and Business accounts. Note that the requested visibility as access
        level for the link may not be granted, depending on the Dropbox folder or team
        settings. Check the returned link metadata to verify the visibility and access
        level.

        :param dbx_path: Dropbox path to file or folder to share.
        :param visibility: The visibility of the shared link. Can be public, team-only,
            or password protected. In case of the latter, the password argument must be
            given. Only available for Professional and Business accounts.
        :param password: Password to protect shared link. Is required if visibility
            is set to password protected and will be ignored otherwise
        :param expires: Expiry time for shared link. Only available for Professional and
            Business accounts.
        :param kwargs: Additional keyword arguments to create the
            :class:`dropbox.sharing.SharedLinkSettings` instance.
        :returns: Metadata for shared link.
        """

        if visibility.is_password() and not password:
            raise MaestralApiError(
                "Invalid shared link setting",
                "Password is required to share a password-protected link",
            )

        if not visibility.is_password():
            password = None

        # Convert timestamp to utc time if not naive.
        if expires is not None:
            has_timezone = expires.tzinfo and expires.tzinfo.utcoffset(expires)
            if has_timezone:
                expires.astimezone(timezone.utc)

        settings = sharing.SharedLinkSettings(
            requested_visibility=visibility,
            link_password=password,
            expires=expires,
            **kwargs,
        )

        with convert_api_errors(dbx_path=dbx_path):
            res = self.dbx.sharing_create_shared_link_with_settings(dbx_path, settings)

        return res

    def revoke_shared_link(self, url: str) -> None:
        """
        Revokes a shared link.

        :param url: URL to revoke.
        """
        with convert_api_errors():
            self.dbx.sharing_revoke_shared_link(url)

    def list_shared_links(
        self, dbx_path: str | None = None
    ) -> sharing.ListSharedLinksResult:
        """
        Lists all shared links for a given Dropbox path (file or folder). If no path is
        given, list all shared links for the account, up to a maximum of 1,000 links.

        :param dbx_path: Dropbox path to file or folder.
        :returns: Shared links for a path, including any shared links for parents
            through which this path is accessible.
        """

        results = []

        with convert_api_errors(dbx_path=dbx_path):
            res = self.dbx.sharing_list_shared_links(dbx_path)
            results.append(res)

            while results[-1].has_more:
                res = self.dbx.sharing_list_shared_links(dbx_path, results[-1].cursor)
                results.append(res)

        return self.flatten_results(results, attribute_name="links")

    @staticmethod
    def flatten_results(
        results: list[PaginationResultType], attribute_name: str
    ) -> PaginationResultType:
        """
        Flattens a list of Dropbox API results from a pagination to a single result with
        the cursor of the last result in the list.

        :param results: List of :results to flatten.
        :param attribute_name: Name of attribute to flatten.
        :returns: Flattened result.
        """

        all_entries = []

        for result in results:
            all_entries += getattr(result, attribute_name)

        kwargs = {
            attribute_name: all_entries,
            "cursor": results[-1].cursor,
            "has_more": False,
        }

        result_cls = type(results[0])
        results_flattened = result_cls(**kwargs)

        return results_flattened
