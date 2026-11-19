"""``ultron doctor [--no-network]`` command."""

from __future__ import annotations

import argparse

from ultron import doctor
from ultron.output import emit


def _run(args: argparse.Namespace) -> int:
    payload = doctor.to_payload(doctor.run_checks(network=not args.no_network))
    emit(payload)
    return 0 if payload["ok"] else 1


def register(subparsers) -> None:
    parser = subparsers.add_parser("doctor", help="check Python, state folder, ledger, extras, install and network")
    parser.add_argument("--no-network", action="store_true", help="skip the DNS and HTTPS checks")
    parser.set_defaults(func=_run)
