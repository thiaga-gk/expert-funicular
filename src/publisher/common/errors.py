"""Shared errors: the refusal exception raised by capabilities and mapped to exit status 2 by the CLI."""


class PublisherError(Exception):
    """A run the publisher refuses: invalid input, an unusable source or a policy refusal.

    The message is written for the operator. The CLI prints it and exits with status 2; library callers catch it.
    """
