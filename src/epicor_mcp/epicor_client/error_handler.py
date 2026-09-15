"""Epicor API error response parsing.

Epicor Kinetic returns errors in several different JSON shapes depending on
the endpoint and error category.  ``ErrorHandler`` normalises all of them
into a single ``EpicorError`` exception and provides a
``format_user_message`` helper that turns technical details into something
suitable for surfacing through the MCP tool layer.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class EpicorError(Exception):
    """Structured representation of an Epicor API error.

    Attributes:
        status_code: HTTP status code from the response.
        message:     Human-readable error summary.
        details:     Optional dict of extra context parsed from the body.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        details: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.details = details or {}
        super().__init__(f"[{status_code}] {message}")


class ErrorHandler:
    """Parse and format Epicor error responses."""

    @staticmethod
    def parse_error(status_code: int, response_body: dict | str) -> EpicorError:
        """Parse an Epicor error response into a structured ``EpicorError``.

        Epicor returns errors in several formats::

            # Format 1 -- business-logic / validation errors
            {"ErrorMessage": "...", "ErrorType": "..."}

            # Format 2 -- HTTP-level rejections (401, 403, etc.)
            {"HttpStatus": 401, "ReasonPhrase": "..."}

            # Format 3 -- OData standard error envelope
            {"error": {"message": "...", "code": "..."}}

            # Format 4 -- plain string (rare, but happens on 502/503)
            "Bad Gateway"

        Parameters
        ----------
        status_code:
            The HTTP status code.
        response_body:
            Parsed JSON dict or raw response text.

        Returns
        -------
        EpicorError
        """
        # Handle plain-string bodies (gateway errors, HTML pages, etc.)
        if isinstance(response_body, str):
            return EpicorError(
                status_code=status_code,
                message=response_body or f"HTTP {status_code}",
            )

        details: dict = {}

        # --- Format 1: ErrorMessage / ErrorType --------------------------
        if "ErrorMessage" in response_body:
            message = response_body["ErrorMessage"]
            error_type = response_body.get("ErrorType", "")
            if error_type:
                details["error_type"] = error_type
            return EpicorError(
                status_code=status_code,
                message=message,
                details=details,
            )

        # --- Format 2: HttpStatus / ReasonPhrase -------------------------
        if "ReasonPhrase" in response_body:
            message = response_body["ReasonPhrase"]
            http_status = response_body.get("HttpStatus", status_code)
            details["http_status"] = http_status
            return EpicorError(
                status_code=status_code,
                message=message,
                details=details,
            )

        # --- Format 3: OData { "error": { "message": ... } } ------------
        if "error" in response_body and isinstance(response_body["error"], dict):
            error_obj = response_body["error"]
            message = error_obj.get("message", str(error_obj))
            code = error_obj.get("code", "")
            if code:
                details["code"] = code
            # Some Epicor OData errors nest inner details.
            innererror = error_obj.get("innererror")
            if innererror:
                details["innererror"] = innererror
            return EpicorError(
                status_code=status_code,
                message=message,
                details=details,
            )

        # --- Fallback: unknown shape ------------------------------------
        return EpicorError(
            status_code=status_code,
            message=f"HTTP {status_code}: {str(response_body)[:500]}",
            details={"raw": response_body},
        )

    @staticmethod
    def format_user_message(error: EpicorError) -> str:
        """Format an ``EpicorError`` into a user-friendly message.

        Translates common HTTP status codes into actionable guidance and
        surfaces the Epicor business-rule message when available.

        Parameters
        ----------
        error:
            The parsed ``EpicorError``.

        Returns
        -------
        str
            A message suitable for returning through the MCP tool layer.
        """
        status = error.status_code

        if status == 401:
            return (
                "Access denied: Your department does not have permission "
                "for this service. Verify the API key scope or contact "
                "your administrator."
            )

        if status == 403:
            return (
                "Forbidden: Your Epicor user account does not have "
                "permission for this operation. Check your Epicor "
                "security group assignments."
            )

        if status == 404:
            return "Record not found. Verify the primary key values and service name."

        if status == 405:
            return (
                f"Method not allowed: {error.message}. "
                "The service does not support this HTTP verb or method."
            )

        if status == 409:
            return (
                f"Conflict: {error.message}. "
                "Another user may have modified this record. "
                "Retrieve the latest version and try again."
            )

        if status == 422:
            return f"Validation error: {error.message}"

        if 500 <= status < 600:
            # Epicor often puts the useful business-rule explanation in
            # ErrorMessage for 500-level responses.
            if error.details.get("error_type"):
                return (
                    f"Epicor business rule error ({error.details['error_type']}): "
                    f"{error.message}"
                )
            return f"Epicor server error: {error.message}"

        # Generic fallback
        return f"Epicor API error (HTTP {status}): {error.message}"
