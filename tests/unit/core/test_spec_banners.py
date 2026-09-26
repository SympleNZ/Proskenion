"""Every §21.26 persistent banner has something that raises it.

§21.26's table lists each condition, its level and its text. On 26 September a
gap audit found two of them (device offline, no Venue Default) raised by
nothing at all, although the interface rendered any banner it was sent. This
reads the table out of the spec and holds each row to the constant that keys
its banner and the module that raises it: a row added to the spec fails here
until it is mapped, and a mapped module that stops calling ``set_banner`` with
its key fails too.

Each banner's behaviour (when it rises, when it clears, its text) is proved by
that module's own tests; this is the index that says none is missing.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from typing import Final

import pytest

from tests.spec_text import section_html, table_rows

#: §21.26 condition -> (module, the constant naming its banner key).
RAISED_BY: Final[dict[str, tuple[str, str]]] = {
    "Device offline": ("proskenion.core.banners", "DEVICE_OFFLINE_KEY"),
    "Multiple offline": ("proskenion.core.banners", "DEVICES_OFFLINE_KEY"),
    "Backup media absent 48h+": ("proskenion.core.health", "BACKUP_BANNER_KEY"),
    "Backup failed, retry also failed": ("proskenion.core.backup", "BACKUP_FAILED_AMBER_KEY"),
    "Backup failed three nights running": ("proskenion.core.backup", "BACKUP_FAILED_RED_KEY"),
    "Certificate expiring": ("proskenion.core.certs", "CERT_EXPIRING_BANNER_KEY"),
    "Certificate expired": ("proskenion.core.certs", "CERT_SELF_SIGNED_BANNER_KEY"),
    "Time not synchronised": ("proskenion.core.timesync", "DEGRADED_BANNER_KEY"),
    "No Venue Default scene": ("proskenion.core.banners", "VENUE_DEFAULT_KEY"),
    "Disk critically low": ("proskenion.core.health", "DISK_BANNER_KEY"),
    "Update rolled back": ("proskenion.core.update_service", "UPDATE_ROLLED_BACK_BANNER"),
    "Update ready": ("proskenion.core.update_service", "UPDATE_READY_BANNER"),
}

#: §21.26's level words, as ``set_banner`` spells them.
LEVELS: Final = {"Red": "red", "Amber": "amber", "Blue": "info"}


def _spec_rows() -> list[list[str]]:
    return table_rows(section_html("persistent-banners", "21.27-empty-error-and-loading-states"))


def _set_banner_calls(scope: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "set_banner"
    ]


def _names(scope: ast.AST) -> set[str]:
    return {node.id for node in ast.walk(scope) if isinstance(node, ast.Name)}


def test_every_banner_in_the_spec_is_mapped() -> None:
    conditions = [row[0] for row in _spec_rows()]
    assert len(conditions) >= 12, "the §21.26 table was not found, or lost rows"
    assert sorted(conditions) == sorted(RAISED_BY), (
        "§21.26's banners and RAISED_BY differ — map the new row to what raises it"
    )


@pytest.mark.parametrize(("condition", "level", "text"), [tuple(row) for row in _spec_rows()])
def test_something_raises_each_banner(condition: str, level: str, text: str) -> None:
    module_name, constant = RAISED_BY[condition]
    module = importlib.import_module(module_name)
    key = getattr(module, constant)
    assert isinstance(key, str) and key, f"{module_name}.{constant} is not a banner key"

    tree = ast.parse(inspect.getsource(module))
    raising = [
        func
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef)
        and constant in _names(func)
        and _set_banner_calls(func)
    ]
    assert raising, f"nothing in {module_name} calls set_banner with {constant} ({text!r})"

    # Where the level is written literally beside the key, it is §21.26's.
    literal_levels = {
        call.args[1].value
        for func in raising
        for call in _set_banner_calls(func)
        if len(call.args) >= 2
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == constant
        and isinstance(call.args[1], ast.Constant)
    }
    assert literal_levels <= {LEVELS[level]}, (
        f"{condition}: §21.26 says {level}, the code raises it as {sorted(literal_levels)}"
    )
