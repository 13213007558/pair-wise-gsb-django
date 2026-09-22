"""SMTP email backend class."""

import smtplib
import ssl
import threading

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.message import sanitize_address
from django.core.mail.utils import DNS_NAME
from django.utils.functional import cached_property


class EmailBackend(BaseEmailBackend):
    """
    A wrapper that manages the SMTP network connection.
    """

    def __init__(
        self,
        host=None,
        port=None,
        username=None,
        password=None,
        use_tls=None,
        fail_silently=False,
        use_ssl=None,
        timeout=None,
        ssl_keyfile=None,
        ssl_certfile=None,
        **kwargs,
    ):
        super().__init__(fail_silently=fail_silently)
        self.host = host or settings.EMAIL_HOST
        self.port = port or settings.EMAIL_PORT
        self.username = settings.EMAIL_HOST_USER if username is None else username
        self.password = settings.EMAIL_HOST_PASSWORD if password is None else password
        self.use_tls = settings.EMAIL_USE_TLS if use_tls is None else use_tls
        self.use_ssl = settings.EMAIL_USE_SSL if use_ssl is None else use_ssl
        self.timeout = settings.EMAIL_TIMEOUT if timeout is None else timeout
        self.ssl_keyfile = (
            settings.EMAIL_SSL_KEYFILE if ssl_keyfile is None else ssl_keyfile
        )
        self.ssl_certfile = (
            settings.EMAIL_SSL_CERTFILE if ssl_certfile is None else ssl_certfile
        )
        if self.use_ssl and self.use_tls:
            raise ValueError(
                "EMAIL_USE_TLS/EMAIL_USE_SSL are mutually exclusive, so only set "
                "one of those settings to True."
            )
        self.connection = None
        self._partial_connection = None
        self._lock = threading.RLock()

    @property
    def connection_class(self):
        return smtplib.SMTP_SSL if self.use_ssl else smtplib.SMTP

    @cached_property
    def ssl_context(self):
        if self.ssl_certfile or self.ssl_keyfile:
            ssl_context = ssl.SSLContext(protocol=ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.load_cert_chain(self.ssl_certfile, self.ssl_keyfile)
            return ssl_context
        else:
            return ssl.create_default_context()

    def open(self):
        """
        Ensure an open connection to the email server. Return whether or not a
        new connection was required (True or False) or None if an exception
        passed silently.
        """
        if self.connection:
            # Nothing to do if the connection is already open.
            return False

        # If a connection was partially opened before, close it.
        if self._partial_connection is not None:
            self._close_connection(self._partial_connection)
            self._partial_connection = None

        # If local_hostname is not specified, socket.getfqdn() gets used.
        # For performance, we use the cached FQDN for local_hostname.
        connection_params = {"local_hostname": DNS_NAME.get_fqdn()}
        if self.timeout is not None:
            connection_params["timeout"] = self.timeout
        if self.use_ssl:
            connection_params["context"] = self.ssl_context
        try:
            self._partial_connection = self.connection_class(
                self.host, self.port, **connection_params
            )

            # TLS/SSL are mutually exclusive, so only attempt TLS over
            # non-secure connections.
            if not self.use_ssl and self.use_tls:
                self._partial_connection.starttls(context=self.ssl_context)
            if self.username and self.password:
                self._partial_connection.login(self.username, self.password)

            # Don't set connection until it's fully configured.
            self.connection = self._partial_connection
            self._partial_connection = None

            return True
        except OSError:
            if not self.fail_silently:
                raise

    def _close_connection(self, connection):
        try:
            connection.quit()
        except (ssl.SSLError, smtplib.SMTPServerDisconnected):
            # This happens when calling quit() on a TLS connection
            # sometimes, or when the connection was already disconnected
            # by the server.
            connection.close()
        except smtplib.SMTPException:
            if self.fail_silently:
                return
            raise

    def close(self):
        """Close the connection to the email server."""
        if self._partial_connection is not None:
            try:
                self._close_connection(self._partial_connection)
            finally:
                self._partial_connection = None
        if self.connection is not None:
            try:
                self._close_connection(self.connection)
            finally:
                self.connection = None

    def send_messages(self, email_messages):
        """
        Send one or more EmailMessage objects and return the number of email
        messages sent.
        """
        if not email_messages:
            return 0
        with self._lock:
            new_conn_created = self.open()
            if not self.connection or new_conn_created is None:
                # We failed silently on open().
                # Trying to send would be pointless.
                return 0
            num_sent = 0
            try:
                for message in email_messages:
                    sent = self._send(message)
                    if sent:
                        num_sent += 1
            finally:
                if new_conn_created:
                    self.close()
        return num_sent

    def _send(self, email_message):
        """A helper method that does the actual sending."""
        if not email_message.recipients():
            return False
        encoding = email_message.encoding or settings.DEFAULT_CHARSET
        use_smtputf8 = getattr(email_message, "use_smtputf8", False)
        from_email = sanitize_address(email_message.from_email, encoding, use_smtputf8)
        recipients = [
            sanitize_address(addr, encoding, use_smtputf8)
            for addr in email_message.recipients()
        ]
        message = email_message.message()
        try:
            mail_options = self._get_mail_options(
                use_smtputf8, [from_email, *recipients]
            )
            self.connection.sendmail(
                from_email,
                recipients,
                message.as_bytes(linesep="\r\n"),
                mail_options=mail_options,
            )
        except smtplib.SMTPException:
            if not self.fail_silently:
                raise
            return False
        return True

    def _get_mail_options(self, use_smtputf8, addresses):
        """
        Return the ESMTP options required to send a message with the given
        envelope addresses, checking that the server supports them.
        """
        if not use_smtputf8 or all(addr.isascii() for addr in addresses):
            # Plain ASCII messages don't require any extension, even on a
            # connection where a previous message used SMTPUTF8.
            return []
        # The message contains non-ASCII email addresses and requires the
        # SMTPUTF8 extension (RFC 6531). Check the extensions advertised by
        # the server in response to the most recent EHLO command, which is
        # issued after the STARTTLS upgrade when TLS is in use.
        self.connection.ehlo_or_helo_if_needed()
        if not self.connection.has_extn("smtputf8"):
            raise smtplib.SMTPNotSupportedError(
                "SMTPUTF8 is required to send this message, but it isn't "
                "supported by the server."
            )
        return ["SMTPUTF8", "BODY=8BITMIME"]
