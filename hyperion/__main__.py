"""Entry point for `python -m hyperion`.

Subcommands
-----------
repl (default)
    python -m hyperion mydb.hyp
    Interactive SQL shell.

server
    python -m hyperion server mydb.hyp [--host HOST] [--port PORT]
    python -m hyperion server mydb.hyp [--socket PATH]
    Start a TCP or Unix-socket server for the database.
"""
import sys


def _run_server(argv: list[str]) -> None:
    import argparse
    from .database import Database
    from .server import Server

    p = argparse.ArgumentParser(
        prog="python -m hyperion server",
        description="Serve a Hyperion database over TCP or a Unix socket.",
    )
    p.add_argument("database", help="Database file path (use :memory: for in-memory)")
    p.add_argument("--host",   default="127.0.0.1", help="TCP bind host (default: 127.0.0.1)")
    p.add_argument("--port",   default=5433, type=int, help="TCP bind port (default: 5433)")
    p.add_argument("--socket", dest="socket_path", default=None,
                   help="Unix-domain socket path (overrides --host/--port)")
    args = p.parse_args(argv)

    db = Database(args.database)
    if args.socket_path:
        srv = Server(db, socket_path=args.socket_path)
        addr = args.socket_path
    else:
        srv = Server(db, host=args.host, port=args.port)
        addr = f"{args.host}:{args.port}"

    print(f"Hyperion server listening on {addr}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        srv.shutdown()
        db.close()


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "server":
        _run_server(sys.argv[2:])
    else:
        from .repl import main as repl_main
        repl_main()


main()
