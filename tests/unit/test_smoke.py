"""Suite sanity check — proves the package imports and the runner is wired."""

import proskenion


def test_package_imports() -> None:
    assert proskenion.__name__ == "proskenion"
