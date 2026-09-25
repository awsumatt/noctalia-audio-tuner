"""Command registry.

Contract for every command module in this package:

    register(sub)   # receive the argparse subparser; add args, then
                    # parser.set_defaults(func=run)
    run(args)       # -> dict payload wrapped by the entrypoint into the
                    #   JSON envelope {"status":"ok","data":<payload>}

The entrypoint globs this package (pkgutil.iter_modules over
tunerlib/commands) and registers every module as a subcommand named after
the module with '_' -> '-'. Human chatter goes to stderr; nothing here may
print to stdout.
"""
