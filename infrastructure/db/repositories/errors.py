"""Errors that carry a message meant for the user."""


class UserError(Exception):
    """A request the user cannot do right now; the message is shown to them as is.

    Handlers catch only this class. Any other exception is a bug or an outage and
    is reported with the generic "try again" message instead of its raw text.
    """
