import logging
import os
import signal
import sys
import threading
from typing import Optional

from netmiko import BaseConnection, ConnectHandler

from .. import BaseDriver
from .model import (
    NetmikoConnectionArgs,
    NetmikoPullingRequest,
    NetmikoPushingRequest,
    NetmikoSendCommandArgs,
    NetmikoSendConfigSetArgs,
)

log = logging.getLogger(__name__)


class NetmikoDriver(BaseDriver):
    """
    This driver has persistent connection support and monitor mechanism.
    But it is not concurrency safe. Only use it with rq.SimpleWorker.
    """

    driver_name = "netmiko"

    persisted_session: BaseConnection = None
    persisted_conn_args: NetmikoConnectionArgs = None

    _monitor_stop_event = None
    _monitor_thread = None
    _monitor_lock = threading.Lock()

    @classmethod
    def _get_persisted_session(cls, conn_args: NetmikoConnectionArgs) -> Optional[BaseConnection]:
        """
        Check if persisted session is still alive, otherwise disconnect it.
        """
        if cls.persisted_session and cls.persisted_conn_args != conn_args:
            if cls.persisted_session:
                log.warning("New connection args detected, disconnecting old session")
                # Stop monitor thread and disconnect
                with cls._monitor_lock:
                    try:
                        cls.persisted_session.disconnect()
                    except Exception as e:
                        log.error(f"Error in disconnecting old session: {e}")
                    finally:
                        cls._set_persisted_session(None, None)

        return cls.persisted_session

    @classmethod
    def _set_persisted_session(
        cls, session: BaseConnection, conn_args: NetmikoConnectionArgs
    ) -> Optional[BaseConnection]:
        """
        Persist session and connection args. Start monitor thread.
        Caller should ensure that the session is disconnected when done.
        """
        # Clear
        if session is None:
            if cls.persisted_conn_args.keepalive:
                cls._stop_monitor_thread()
            cls.persisted_session = None
            cls.persisted_conn_args = None
            return None

        # Setup
        cls.persisted_session = session
        cls.persisted_conn_args = conn_args
        cls._start_monitor_thread(cls.persisted_session)

        return cls.persisted_session

    @classmethod
    def _start_monitor_thread(cls, session: BaseConnection):
        """
        session.is_alive() will send NULL to device. We rely on this to keepalive.
        However, BaseConnection is not concurrency safe, we have to use a lock.
        """
        if cls._monitor_thread and cls._monitor_thread.is_alive():
            log.info("Monitoring thread already running")
            return

        cls._monitor_stop_event = threading.Event()
        host = cls.persisted_conn_args.host
        timeout = cls.persisted_conn_args.keepalive

        def monitor():
            suicide = False
            log.info(f"Monitoring thread started ({host})")

            while not cls._monitor_stop_event.is_set():
                if cls._monitor_stop_event.wait(timeout=timeout):
                    break

                with cls._monitor_lock:
                    # Double checking
                    if cls._monitor_stop_event.is_set():
                        break

                    # Health checking
                    if not session.is_alive():
                        log.warning(f"Connection to {host} is unhealthy")
                        suicide = True
                        cls._monitor_stop_event.set()
                        break

                    # Keepalive
                    try:
                        if junk := session.clear_buffer():
                            log.debug(f"Detected junk data in keepalive: {junk}")
                        session.write_channel(session.RETURN)
                    except Exception as e:
                        log.warning(f"Error in sending keepalive to {host}: {e}")
                        suicide = True
                        cls._monitor_stop_event.set()

            log.debug(f"Monitoring thread quitting with `suicide={suicide}`.")

            # When connection is disconnected, the worker should suicide.
            if suicide:
                log.info(f"Pinned worker for {host} suicides. ")
                os.kill(os.getpid(), signal.SIGTERM)

            # This only exits from current thread
            sys.exit(0)

        cls._monitor_thread = threading.Thread(target=monitor, daemon=True)
        cls._monitor_thread.start()

    @classmethod
    def _stop_monitor_thread(cls):
        """Stop the monitor thread."""
        if cls._monitor_stop_event:
            cls._monitor_stop_event.set()
        if cls._monitor_thread and cls._monitor_thread.is_alive():
            cls._monitor_thread.join()
        cls._monitor_thread = None
        cls._monitor_stop_event = None

    @classmethod
    def from_pulling_request(cls, req: NetmikoPullingRequest):
        # Pydantic don't have implicit conversion, we have to do it explicitly
        if not isinstance(req, NetmikoPullingRequest):
            req = NetmikoPullingRequest.model_validate(obj=req.model_dump())
        return cls(args=req.args, conn_args=req.connection_args, enabled=req.enable_mode)

    @classmethod
    def from_pushing_request(cls, req: NetmikoPushingRequest):
        if not isinstance(req, NetmikoPushingRequest):
            req = NetmikoPushingRequest.model_validate(obj=req.model_dump())
        return cls(
            args=req.args,
            conn_args=req.connection_args,
            enabled=req.enable_mode,
            save=req.save,
        )

    def __init__(
        self,
        args: NetmikoSendCommandArgs | NetmikoSendConfigSetArgs,
        conn_args: NetmikoConnectionArgs,
        enabled: bool = False,
        save: bool = True,
        **kwargs,
    ):
        self.args = args
        self.conn_args = conn_args
        self.enabled = enabled
        self.save = save

    def connect(self) -> BaseConnection:
        try:
            session = self._get_persisted_session(self.conn_args)
            if session:
                log.info("Reusing existing connection")
            else:
                log.info(f"Creating new connection to {self.conn_args.host}...")
                session = ConnectHandler(**self.conn_args.model_dump())
                if self.conn_args.keepalive:
                    self._set_persisted_session(session, self.conn_args)
            return session
        except Exception as e:
            log.error(f"Error in connecting: {e}")
            raise e

    def send(self, session: BaseConnection = None, command: Optional[list[str]] = None):
        try:
            with self._monitor_lock:
                if self.enabled:
                    session.enable()

                result = {}
                for cmd in command:
                    if self.args:
                        response = session.send_command(cmd, **self.args.model_dump())
                    else:
                        response = session.send_command(cmd)
                    result[cmd] = response

                if self.enabled:
                    session.exit_enable_mode()

            return result
        except Exception as e:
            log.error(f"Error in sending command: {e}")
            raise e

    def config(
        self,
        session: BaseConnection = None,
        config: Optional[list[str]] = None,
    ):
        """
        Send -> (Commit) -> Save
        Some devices may not support commit.
        """
        try:
            with self._monitor_lock:
                if self.enabled:
                    session.enable()

                if self.args:
                    response = [session.send_config_set(config, **self.args.model_dump())]
                else:
                    response = [session.send_config_set(config)]

                if commit := self._commit(session):
                    response.append(commit)

                if self.save:
                    session.set_base_prompt()
                    response.append(session.save_config())

                if self.enabled:
                    session.exit_enable_mode()

            return response
        except Exception as e:
            log.error(f"Error in sending config: {e}")
            raise e

    def _commit(self, session: BaseConnection) -> Optional[str]:
        """
        Commit the configuration.
        This should be called after sending the configuration.

        NOTE: Caller should own the lock!
        NOTE: Some devices may not support commit. In this case, the running-config
        is already updated.
        """
        result = None
        try:
            result = session.commit()
        except (NotImplementedError, AttributeError):
            pass
        return result

    def disconnect(self, session: BaseConnection, reset=False):
        """
        Disconnect the session and stop monitor thread.
        """
        # We only disconnect if reset is True, so that we can reuse the connection
        if not reset:
            return

        with self._monitor_lock:
            try:
                # Stop monitor thread and disconnect
                session.disconnect()
            except Exception as e:
                log.error(f"Error in disconnecting (reset): {e}")
                raise e
            finally:
                self._set_persisted_session(None, self.conn_args)


__all__ = ["NetmikoDriver"]
