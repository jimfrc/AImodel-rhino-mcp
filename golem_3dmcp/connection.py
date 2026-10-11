"""
golem_3dmcp/connection.py
========================
TCP client manager for communicating with the GOLEM-3DMCP Rhino plugin server.

Design decisions:
  - Singleton: Only one RhinoConnection instance exists per process.  The MCP
    server is single-process and needs a single, persistent connection to one
    Rhino instance.
  - Thread-safe: A threading.Lock guards every send/receive operation.  The MCP
    framework may call tools from multiple threads concurrently.
  - Auto-reconnect: Transient errors (BrokenPipeError, ConnectionResetError)
    trigger up to 3 automatic reconnect attempts before propagating.
  - Request IDs: Every outbound command gets a UUID4 and the plugin echoes it
    back in the response envelope.  The reply's id is checked against the
    request's id on every command; a mismatch means the stream has
    desynchronised and raises RhinoProtocolError rather than returning
    another command's result.
  - Timeouts are terminal for the socket: a timed-out reply is still in
    flight, so the socket is closed and the connection marked disconnected.
    The next command reconnects onto a fresh stream instead of reading the
    stale reply.

Author: GOLEM-3DMCP
"""

import socket
import threading
import uuid
from typing import Any

from golem_3dmcp.protocol import recv_message, send_message

# ---------------------------------------------------------------------------
# Custom exception hierarchy
# ---------------------------------------------------------------------------

class RhinoConnectionError(OSError):
    """
    Raised when a TCP connection to the Rhino plugin server cannot be
    established or is unexpectedly lost.
    """


class RhinoCommandError(RuntimeError):
    """
    Raised when the Rhino plugin returns an error-level response to a command
    (i.e., the 'error' field in the response envelope is non-null).

    Attributes:
        code:    Integer error code from the Rhino plugin.
        message: Human-readable error description from the Rhino plugin.
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"Rhino error {code}: {message}")
        self.code = code
        self.message = message


class RhinoTimeoutError(TimeoutError):
    """
    Raised when a command sent to Rhino does not receive a response within
    the specified timeout period.

    The connection is closed before this is raised: the reply is still in
    flight, so the stream can no longer be trusted to be in sync.
    """


class RhinoProtocolError(RhinoConnectionError):
    """
    Raised when a response violates the request/response contract — in
    practice, when the 'id' field of the response does not match the 'id'
    of the request it is supposed to answer.

    This means the reply stream is one or more messages out of step with
    the request stream, so the payload just read belongs to a *different*
    command.  Returning it would hand the caller another command's result
    as if it were its own.  The connection is closed before this is raised.

    Attributes:
        request_id:  The id sent with the request.
        response_id: The id carried by the response that was read.
    """

    def __init__(self, request_id: str, response_id: Any, method: str) -> None:
        super().__init__(
            f"Response id mismatch for '{method}': expected {request_id!r}, "
            f"got {response_id!r}.  The connection is out of sync and has "
            f"been closed; the next command will reconnect."
        )
        self.request_id = request_id
        self.response_id = response_id


# ---------------------------------------------------------------------------
# RhinoConnection
# ---------------------------------------------------------------------------

class RhinoConnection:
    """
    Manages a persistent TCP connection to the GOLEM-3DMCP Rhino plugin server.

    Usage:
        conn = get_connection()
        result = conn.send_command("scene.get_all_objects", {})

    Thread safety:
        send_command() acquires a lock for the duration of the send+receive
        cycle, so concurrent callers are serialised.  This matches the
        Rhino plugin's single-client-at-a-time server model.
    """

    _MAX_AUTO_RECONNECT_ATTEMPTS = 3

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._host: str = "127.0.0.1"
        self._port: int = 9876
        # True once connect() has succeeded at least once.  Only then may
        # send_command() re-establish a connection on its own; a connection
        # that was never opened still reports "call connect() first".
        self._ever_connected: bool = False

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(
        self,
        host: str = "127.0.0.1",
        port: int = 9876,
        timeout: int = 10,
    ) -> None:
        """
        Establish a TCP connection to the Rhino plugin server and verify it
        is alive by sending a 'ping' command.

        Args:
            host:    Hostname or IP address of the Rhino machine (default:
                     localhost).
            port:    TCP port the Rhino plugin is listening on (default: 9876).
            timeout: Connection timeout in seconds (default: 10).

        Raises:
            RhinoConnectionError: If the TCP connection cannot be established
                or the ping handshake fails.
        """
        with self._lock:
            self._host = host
            self._port = port
            self._sock = self._create_socket(host, port, timeout)

        # Verify the server is alive and responding correctly.
        # send_command() will acquire the lock internally.
        try:
            self.send_command("ping", {}, timeout=timeout)
        except Exception as exc:
            self.disconnect()
            raise RhinoConnectionError(
                f"Connected to {host}:{port} but ping handshake failed: {exc}"
            ) from exc

        self._ever_connected = True

    def _create_socket(self, host: str, port: int, timeout: int) -> socket.socket:
        """
        Create and connect a TCP socket.  Returns the connected socket.

        Raises:
            RhinoConnectionError: If the TCP handshake fails.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
        except (TimeoutError, OSError) as exc:
            sock.close()
            raise RhinoConnectionError(
                f"Cannot connect to Rhino plugin at {host}:{port}: {exc}"
            ) from exc
        # Switch to blocking (no timeout) after connection — individual
        # commands specify their own timeout via socket.settimeout().
        sock.settimeout(None)
        return sock

    def disconnect(self) -> None:
        """
        Close the TCP connection cleanly.

        Safe to call even if already disconnected.
        """
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        """
        Internal: close the socket and mark the connection disconnected.

        The caller must already hold self._lock (which is not reentrant).
        """
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def reconnect(self) -> None:
        """
        Close the existing connection and establish a fresh one using the
        same host/port as the most recent connect() call.

        Raises:
            RhinoConnectionError: If the re-connection fails.
        """
        self.disconnect()
        self.connect(host=self._host, port=self._port)

    def is_connected(self) -> bool:
        """
        Check whether the socket is believed to be connected.

        This is a lightweight heuristic — it does not perform a round-trip
        to Rhino.  A socket that has been disconnected by the remote peer
        may still appear connected until the next send/recv.  For a hard
        liveness check, call send_command("ping", {}).

        Returns:
            True if a socket object exists and has not been explicitly closed.
        """
        with self._lock:
            return self._sock is not None

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def send_command(
        self,
        method: str,
        params: dict[str, Any],
        timeout: int = 30,
    ) -> dict[str, Any]:
        """
        Send a command to Rhino and wait for the response.

        The request envelope follows the server's expected format:
            {"id": "<uuid4>", "method": "<method>", "params": {...}}

        The response envelope is expected to be:
            {"id": "<uuid4>", "result": {...}, "error": null}
          or
            {"id": "<uuid4>", "result": null,  "error": {"code": N, "message": "..."}}

        Args:
            method:  The Rhino plugin method name (e.g., "scene.get_objects").
            params:  Dictionary of parameters for the method.
            timeout: Seconds to wait for a response before raising
                     RhinoTimeoutError (default: 30).

        Returns:
            The "result" dictionary from the response envelope.

        Raises:
            RhinoConnectionError: If no connection exists or the connection
                is lost mid-command and auto-reconnect fails.
            RhinoCommandError:    If Rhino reports an error for this command.
            RhinoTimeoutError:    If no response arrives within *timeout* seconds.
                The connection is closed before this is raised.
            RhinoProtocolError:   If the response id does not match the request
                id.  The connection is closed before this is raised.
        """
        request_id = str(uuid.uuid4())
        request = {
            "id": request_id,
            "method": method,
            "params": params,
        }

        last_exc: Exception | None = None
        for attempt in range(1, self._MAX_AUTO_RECONNECT_ATTEMPTS + 1):
            try:
                if self._ever_connected and not self.is_connected():
                    # A previous command closed the socket (timeout or an
                    # out-of-sync reply).  Start this one on a fresh stream
                    # rather than on a stream that is a message behind.
                    self.reconnect()
                return self._send_and_recv(request, timeout)
            except ConnectionError as exc:
                # 内建 ConnectionError 覆盖 BrokenPipe / ConnectionReset /
                # ConnectionAborted(10053) / ConnectionRefused(10061)，
                # 以及 protocol.py 读到 EOF 抛的 ConnectionError。
                # 原来只列了前两种，导致连接被中止后坏 socket 一直复用、永久失败。
                last_exc = exc
                if attempt < self._MAX_AUTO_RECONNECT_ATTEMPTS:
                    # Try to reconnect and retry the command.
                    try:
                        self.reconnect()
                    except RhinoConnectionError:
                        # Reconnect failed; keep trying up to max attempts.
                        pass
            except RhinoTimeoutError:
                raise  # Timeouts are not retried.
            except RhinoProtocolError:
                raise  # A desynchronised stream is not retried.
            except RhinoCommandError:
                raise  # Application-level errors are not retried.

        raise RhinoConnectionError(
            f"Command '{method}' failed after "
            f"{self._MAX_AUTO_RECONNECT_ATTEMPTS} attempts: {last_exc}"
        ) from last_exc

    def _send_and_recv(self, request: dict[str, Any], timeout: int) -> dict[str, Any]:
        """
        Internal: acquire the lock, send *request*, receive one response.

        Raises:
            RhinoConnectionError: If not connected.
            RhinoTimeoutError:    If socket times out waiting for a response.
            RhinoCommandError:    If the response contains an error.
            OSError:              On other socket-level failures (callers should
                                  detect BrokenPipeError / ConnectionResetError
                                  and retry).
        """
        with self._lock:
            if self._sock is None:
                raise RhinoConnectionError(
                    "Not connected to Rhino plugin.  Call connect() first."
                )

            # Apply per-command timeout for the receive phase.
            self._sock.settimeout(timeout)
            try:
                send_message(self._sock, request)
                response = recv_message(self._sock)
            except TimeoutError as exc:
                # The reply we gave up on is still in flight.  Leaving the
                # socket open would put the read side one message behind for
                # every later command, so drop it: is_connected() goes False
                # and the next send_command() reconnects onto a clean stream.
                self._close_locked()
                raise RhinoTimeoutError(
                    f"Rhino did not respond to '{request['method']}' "
                    f"within {timeout}s.  Connection closed; the next "
                    f"command will reconnect."
                ) from exc
            finally:
                # Reset to blocking (no timeout) after this command.
                if self._sock is not None:
                    self._sock.settimeout(None)

        # ------------------------------------------------------------------
        # Validate and unpack the response.
        # ------------------------------------------------------------------
        if not isinstance(response, dict):
            raise RhinoConnectionError(
                f"Malformed response (expected dict, got {type(response).__name__})"
            )

        # ------------------------------------------------------------------
        # Correlation check.  The plugin echoes the request id back
        # (_rhino_plugin/server.py builds every envelope with
        # "id": request_id).  If what came back is not the answer to what we
        # asked, the stream is desynchronised — close it and say so, rather
        # than handing the caller some other command's result.
        # ------------------------------------------------------------------
        response_id = response.get("id")
        if response_id != request["id"]:
            self.disconnect()
            raise RhinoProtocolError(
                request_id=request["id"],
                response_id=response_id,
                method=request["method"],
            )

        error = response.get("error")
        if error is not None:
            code = error.get("code", -1) if isinstance(error, dict) else -1
            message = (
                error.get("message", str(error))
                if isinstance(error, dict)
                else str(error)
            )
            raise RhinoCommandError(code=code, message=message)

        result = response.get("result")
        if result is None:
            result = {}

        return result


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_singleton: RhinoConnection | None = None
_singleton_lock = threading.Lock()


def get_connection(
    host: str | None = None,
    port: int | None = None,
    timeout: int = 10,
) -> RhinoConnection:
    """
    Return the singleton RhinoConnection, connecting automatically if needed.

    On the first call (or after the singleton was disconnected and garbage-
    collected), a new RhinoConnection is created and connect() is called.
    Subsequent calls return the same instance without reconnecting.

    Host and port default to values from golem_3dmcp.config if not supplied.

    Args:
        host:    Rhino plugin host (defaults to config value).
        port:    Rhino plugin port (defaults to config value).
        timeout: Connection timeout in seconds (default: 10).

    Returns:
        An active RhinoConnection instance.

    Raises:
        RhinoConnectionError: If the connection cannot be established.
    """
    global _singleton

    # Resolve defaults from config.
    if host is None or port is None:
        try:
            from golem_3dmcp import config as _cfg
            _default_host = getattr(_cfg, "RHINO_HOST", "127.0.0.1")
            _default_port = getattr(_cfg, "RHINO_PORT", 9876)
        except ImportError:
            _default_host = "127.0.0.1"
            _default_port = 9876
        host = host or _default_host
        port = port or _default_port

    with _singleton_lock:
        if _singleton is None or not _singleton.is_connected():
            conn = RhinoConnection()
            conn.connect(host=host, port=port, timeout=timeout)
            _singleton = conn
        return _singleton
