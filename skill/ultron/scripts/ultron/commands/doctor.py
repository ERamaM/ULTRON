"""``ultron doctor [--no-network] [--public]`` command."""

from __future__ import annotations

import argparse

from ultron.commands.output import emit, fail
from ultron.diagnostics import doctor


def _run(args: argparse.Namespace) -> int:
    if args.public:
        try:
            payload = doctor.to_public_payload(doctor.run_checks(network=not args.no_network))
        except Exception:  # noqa: BLE001 - exception text may contain private local data
            return fail("diagnostic_failed", "The public diagnostic could not be completed.", code=1)
    else:
        payload = doctor.to_payload(doctor.run_checks(network=not args.no_network))
    emit(payload)
    return 0 if payload["ok"] else 1


def register(subparsers) -> None:
    """Register the ``ultron doctor`` command."""
    parser = subparsers.add_parser("doctor", help="check Python, state folder, ledger, extras, install and network")
    parser.add_argument("--no-network", action="store_true", help="skip the DNS and HTTPS checks")
    parser.add_argument("--public", action="store_true", help="omit local paths, check details and exception text")
    parser.set_defaults(func=_run)
