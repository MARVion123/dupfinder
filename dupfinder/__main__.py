"""Command line entry point.

    python3 -m dupfinder serve                      # start the web UI
    python3 -m dupfinder scan /volume1/photo        # headless scan
    python3 -m dupfinder report --top 20            # print the last results
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.parse

from . import __version__
from .config import Config
from .db import Database
from .scanner import ScanEngine


def human(n):
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024.0:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f EiB" % n


def cmd_serve(args, config):
    from .server import serve

    if args.port:
        config.update({"port": args.port})
    if args.host:
        config.update({"host": args.host})
    if args.token is not None:
        config.update({"auth_token": args.token})

    app, httpd = serve(config)
    host, port = httpd.server_address[0], httpd.server_address[1]
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print("dupfinder %s listening on http://%s:%s" % (__version__, shown, port))
    print("  data dir : %s" % config.data_dir)
    print("  auth     : %s" % ("token required" if config["auth_token"] else "OPEN - set a token if this port is reachable"))
    ai = app.ai.availability()
    print("  ai       : %s" % (config["ai_model"] if ai["ok"] else "unavailable (%s)" % ai["reason"]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        app.scanner.cancel()
        httpd.shutdown()
    return 0


def cmd_scan(args, config):
    db = Database(config.db_path)
    engine = ScanEngine(db, config)
    options = {}
    if args.no_verify:
        options["verify_bytes"] = False
    if args.no_near:
        options["near_duplicates"] = False
    if args.threshold:
        options["near_threshold"] = args.threshold
    if args.quick:
        options["quick_rescan"] = True
    if args.no_quick:
        options["quick_rescan"] = False

    scan_id = engine.start(args.path, options)
    print("scan #%d  %s" % (scan_id, args.path))
    last = ""
    try:
        while engine.is_running():
            st = engine.status()
            line = "  [%d/%d] %-28s %7s files  %10s  %s" % (
                st["phase_index"], st["phase_total"], st["phase_label"],
                st["files_seen"], human(st["bytes_hashed"]),
                ("%.0f%%" % st["phase_progress"]) if st["phase_progress"] is not None else "",
            )
            if line != last:
                sys.stdout.write("\r" + line.ljust(100))
                sys.stdout.flush()
                last = line
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\ncancelling...")
        engine.cancel()
        while engine.is_running():
            time.sleep(0.2)

    st = engine.status()
    print("\n  state: %s   groups: %s   reclaimable: %s   elapsed: %.1fs"
          % (st["state"], st["groups_found"], human(st["wasted_bytes"]), st["elapsed"]))
    if st.get("error"):
        print("  error: %s" % st["error"])
        return 1

    if not args.no_suggest:
        from .ai import heuristic_suggestions

        written = heuristic_suggestions(db, scan_id)
        print("  suggestions written for %d groups" % written)
    print("  browse the results with:  python3 -m dupfinder report --scan %d" % scan_id)
    return 0


def cmd_report(args, config):
    db = Database(config.db_path)
    if args.scan:
        scan_id = args.scan
    else:
        row = db.one("SELECT id FROM scans ORDER BY id DESC LIMIT 1")
        if not row:
            print("No scans yet.")
            return 1
        scan_id = row["id"]

    scan = db.one("SELECT * FROM scans WHERE id=?", (scan_id,))
    print("scan #%d  %s  [%s]" % (scan["id"], scan["root"], scan["state"]))
    print("  %s files, %s indexed, %s groups, %s reclaimable\n" % (
        scan["files_seen"], human(scan["bytes_seen"]),
        scan["groups_found"], human(scan["wasted_bytes"])))

    groups = db.query(
        """SELECT g.*, s.summary, s.source FROM groups g
           LEFT JOIN suggestions s ON s.group_id=g.id
           WHERE g.scan_id=? ORDER BY g.wasted_bytes DESC LIMIT ?""",
        (scan_id, args.top),
    )
    for group in groups:
        flag = "exact" if group["kind"] == "exact" else "near "
        verified = "verified" if group["verified"] else ""
        print("#%-6d %s %5.1f%% %-9s %2d files  %10s reclaimable  %s" % (
            group["id"], flag, group["similarity"], verified,
            group["file_count"], human(group["wasted_bytes"]),
            group["summary"] or ""))
        for member in db.query(
            """SELECT f.path, f.size, m.similarity FROM group_members m
               JOIN files f ON f.id=m.file_id WHERE m.group_id=? ORDER BY f.size DESC""",
            (group["id"],),
        ):
            print("        %5.1f%%  %9s  %s" % (
                member["similarity"], human(member["size"]), member["path"]))
        print()
    return 0


def _human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024


def cmd_usage(args, config):
    """Show what the database is holding, and where it came from."""
    db = Database(config.db_path)
    info = db.usage()
    print("database: %s" % config.db_path)
    print("on disk : %s\n" % _human(info["bytes_on_disk"]))
    print("  %-16s %12s" % ("table", "rows"))
    for name, count in info["counts"].items():
        print("  %-16s %12s" % (name, "{:,}".format(count)))
    if info["scans"]:
        print("\n  %-4s %-10s %10s %8s  %s" % ("scan", "state", "files", "groups", "root"))
        for scan in info["scans"]:
            print("  #%-3d %-10s %10s %8s  %s" % (
                scan["id"], scan["state"], "{:,}".format(scan["files"]),
                "{:,}".format(scan["groups"]), scan["root"]))
        print("\nEvery scan keeps a full copy of the file index. "
              "`dupfinder prune` drops the older ones.")
    return 0


def cmd_prune(args, config):
    db = Database(config.db_path)
    before = db.usage()
    doomed = max(0, len(before["scans"]) - max(0, args.keep))
    if not doomed and args.keep_cache:
        print("Nothing to drop: %d scan(s), keeping %d." % (len(before["scans"]), args.keep))
        return 0
    if not args.yes:
        print("This will drop %d of %d scans and their results, keeping the "
              "%d most recent." % (doomed, len(before["scans"]), args.keep))
        if not args.keep_cache:
            print("Cache entries for files that no longer exist will go too.")
        if args.vacuum:
            print("The database will then be rewritten, which needs roughly "
                  "%s free on the volume." % _human(before["bytes_on_disk"]))
        if input("Continue? [y/N] ").strip().lower() not in ("y", "j", "yes", "ja"):
            print("Cancelled.")
            return 1

    removed = db.prune(keep_scans=args.keep, drop_stale_cache=not args.keep_cache)
    print("dropped %d scan(s), %s file rows, %s groups, %s stale cache entries"
          % (removed["scans"], "{:,}".format(removed["files"]),
             "{:,}".format(removed["groups"]), "{:,}".format(removed["cache"])))
    if args.vacuum:
        print("rewriting the database ...")
        db.vacuum()
    after = db.usage()
    print("on disk : %s -> %s%s" % (
        _human(before["bytes_on_disk"]), _human(after["bytes_on_disk"]),
        "" if args.vacuum else "   (run again with --vacuum to actually shrink the file)"))
    return 0


def cmd_helper(args, config):
    """Hash windows for a NAS that asked for help.

    Runs on a desktop, which is roughly forty times faster at the byte hash
    than a NAS is. It never learns a path: it asks for work, receives bytes and
    returns a signature. If the server goes away it keeps retrying quietly,
    because the interesting case is a scan that runs for hours.
    """
    import json as _json
    import urllib.error
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    from . import hashing

    base = args.server.rstrip("/")
    headers = {}
    if args.token:
        headers["Authorization"] = "Bearer " + args.token

    def request(path, data=None):
        url = base + path
        body = _json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=dict(headers))
        if body is not None:
            req.add_header("Content-Type", "application/json")
        return urllib.request.urlopen(req, timeout=args.timeout)

    def one_job():
        """Claim, hash, return. True if there was work."""
        try:
            with request("/api/work/claim?helper=" + urllib.parse.quote(args.name)) as resp:
                if resp.status == 204:
                    return False
                job_id = resp.headers.get("X-Job-Id")
                size = int(resp.headers.get("X-Job-Size") or 0)
                max_bytes = int(resp.headers.get("X-Job-Max-Bytes") or 0)
                data = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise SystemExit(
                    "The server is not accepting helpers. Switch on "
                    "'Use another machine' in its settings.")
            raise
        if not job_id:
            return False
        signature = hashing.fuzzy_hash_window(data, size, max_bytes)
        request("/api/work/result",
                {"job_id": job_id, "fuzzy": signature}).close()
        return True

    print("helper %s -> %s" % (args.name, base))
    print("  %d worker(s). Stop with Ctrl-C; the NAS carries on without it."
          % args.workers)
    done = 0
    idle_since = None
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        try:
            while True:
                futures = [pool.submit(one_job) for _ in range(args.workers)]
                worked = 0
                for future in futures:
                    try:
                        worked += 1 if future.result() else 0
                    except (urllib.error.URLError, OSError) as exc:
                        # A NAS that reboots mid-scan, a cable pulled out. Say
                        # it once and keep trying: giving up would strand a
                        # scan that is still running perfectly well.
                        if idle_since is None:
                            print("  waiting for %s (%s)" % (base, exc))
                            idle_since = time.time()
                        break
                if worked:
                    done += worked
                    idle_since = None
                    sys.stdout.write("\r  %d windows hashed" % done)
                    sys.stdout.flush()
                else:
                    time.sleep(args.poll)
        except KeyboardInterrupt:
            print("\n  stopped after %d windows" % done)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="dupfinder",
        description="Deep duplicate finder for Synology DSM 7.4")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--data-dir", default=os.environ.get("DUPFINDER_DATA_DIR"),
                        help="where the database and config live")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="run the web UI")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--token", help="require this bearer token on every request")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("scan", help="scan a directory from the shell")
    p.add_argument("path")
    p.add_argument("--no-verify", action="store_true",
                   help="skip byte-for-byte confirmation of MD5 matches")
    p.add_argument("--no-near", action="store_true",
                   help="skip the fuzzy near-duplicate pass")
    p.add_argument("--threshold", type=int, help="near-duplicate cutoff, 0-100")
    p.add_argument("--quick", action="store_true",
                   help="reuse folders whose mtime has not moved since the last "
                        "completed scan; only safe if files are never edited in place")
    p.add_argument("--no-quick", action="store_true",
                   help="force a full re-index even if quick_rescan is configured on")
    p.add_argument("--no-suggest", action="store_true")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("usage", help="show what the database is holding")
    p.set_defaults(func=cmd_usage)

    p = sub.add_parser("prune", help="drop old scans and stale cache entries")
    p.add_argument("--keep", type=int, default=3,
                   help="how many of the most recent scans to keep (default 3)")
    p.add_argument("--keep-cache", action="store_true",
                   help="do not drop cache entries whose file no longer exists")
    p.add_argument("--vacuum", action="store_true",
                   help="rewrite the database so the freed space is returned "
                        "to the volume; needs the current size free and must "
                        "not run during a scan")
    p.add_argument("--yes", action="store_true", help="do not ask")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser(
        "helper",
        help="lend this machine's CPU to a NAS running a scan")
    p.add_argument("--server", required=True,
                   help="http://<nas>:8777 - the address you open the UI at")
    p.add_argument("--token", help="access token, if the server requires one")
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1),
                   help="parallel requests (default: one per core less one)")
    p.add_argument("--name", default=os.environ.get("COMPUTERNAME")
                   or os.environ.get("HOSTNAME") or "helper",
                   help="how this machine shows up in the scan status")
    p.add_argument("--poll", type=float, default=2.0,
                   help="seconds to wait when there is no work")
    p.add_argument("--timeout", type=float, default=120.0)
    p.set_defaults(func=cmd_helper)

    p = sub.add_parser("report", help="print results of a scan")
    p.add_argument("--scan", type=int)
    p.add_argument("--top", type=int, default=25)
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    config = Config(args.data_dir)
    return args.func(args, config)


if __name__ == "__main__":
    sys.exit(main())
