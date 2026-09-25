"""tunerlib — headless backend library for the Noctalia audio tuner.

All command modules live in tunerlib/commands and expose:

    register(sub)   # add an argparse subparser with set_defaults(func=run)
    run(args)       # return a dict payload (JSON data for the envelope)

Human-readable chatter goes to stderr; the entrypoint prints exactly one JSON
envelope to stdout (see README.md for the protocol and progress contract).
"""


class TunerError(Exception):
    """Error carrying a machine-readable code for the JSON envelope."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def error_envelope(exc: TunerError) -> dict:
    return {"status": "error", "data": None,
            "error": {"code": exc.code, "message": exc.message}}


def ok_envelope(data: dict) -> dict:
    return {"status": "ok", "data": data, "error": None}
