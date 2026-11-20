import json

from ultron import __version__
from ultron.cli import main


def test_version_prints_json(capsys):
    assert main(["version"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ultron": __version__}


def test_unknown_command_fails(capsys):
    assert main(["nope"]) != 0
