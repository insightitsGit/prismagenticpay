"""Release hygiene: no real secrets in source or built artifacts."""
from __future__ import annotations

import re
import tarfile
import zipfile
from pathlib import Path

import pytest

from prismagenticpay.config import Settings

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "prismagenticpay"

# Real provider/token material — not short fixture strings such as sk_test_fixture.
SECRET_RE = re.compile(
    r"""(?x)
    sk_(?:live|test)_[0-9A-Za-z]{20,}
    | pk_(?:live|test)_[0-9A-Za-z]{20,}
    | rk_(?:live|test)_[0-9A-Za-z]{20,}
    | whsec_[0-9A-Za-z]{16,}
    | pypi-[A-Za-z0-9_-]{20,}
    | gh[pousr]_[A-Za-z0-9]{20,}
    | github_pat_[A-Za-z0-9_]{20,}
    | -----BEGIN[ A-Z]*PRIVATE KEY-----
    """
)
FORBIDDEN_NAMES = {".env", ".pypirc", "id_rsa", "id_ed25519"}


def _iter_text_files(root: Path):
    skip_dirs = {".git", ".venv", "venv", "dist", "build", "__pycache__", ".pytest_cache", ".mypy_cache"}
    for path in root.rglob("*"):
        if any(part in skip_dirs for part in path.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2"}:
            continue
        yield path


def test_settings_repr_does_not_include_secrets():
    settings = Settings(
        stripe_api_key="sk_test_should_never_appear_in_repr",
        coinbase_api_key="cb_secret_value",
        sap_token="sap-secret",
        netsuite_token="ns-secret",
        coupa_token="coupa-secret",
        signing_seed_hex="ab" * 32,
        api_keys=["super-secret-api-key"],
    )
    rendered = repr(settings)
    assert "sk_test_should_never_appear_in_repr" not in rendered
    assert "cb_secret_value" not in rendered
    assert "sap-secret" not in rendered
    assert "ns-secret" not in rendered
    assert "coupa-secret" not in rendered
    assert "abababab" not in rendered
    assert "super-secret-api-key" not in rendered
    assert settings.stripe_api_key == "sk_test_should_never_appear_in_repr"


def test_package_source_has_no_embedded_secrets():
    hits = []
    for path in _iter_text_files(PACKAGE):
        text = path.read_text(encoding="utf-8", errors="replace")
        if SECRET_RE.search(text):
            hits.append(str(path.relative_to(ROOT)))
        if path.name in FORBIDDEN_NAMES:
            hits.append(str(path.relative_to(ROOT)))
    assert hits == []


def test_dotenv_example_has_empty_secret_slots():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "STRIPE_API_KEY=" in example
    assert re.search(r"^STRIPE_API_KEY=\s*$", example, re.M)
    assert re.search(r"^PAP_SIGNING_SEED_HEX=\s*$", example, re.M)
    assert SECRET_RE.search(example) is None


def test_built_artifacts_exclude_secrets_and_operator_files():
    dist = ROOT / "dist"
    archives = list(dist.glob("*.whl")) + list(dist.glob("*.tar.gz"))
    if not archives:
        pytest.skip("dist/ is empty; build before checking packaged artifacts")
    forbidden_fragments = (
        ".env",
        "COMPANY_STRIPE",
        "set_stripe_sandbox_secret",
        "publish_pypi",
        "id_rsa",
        ".pypirc",
    )
    for archive in archives:
        names, texts = _archive_entries(archive)
        assert any(name.endswith("prismagenticpay/console/index.html") or
                   name.endswith("prismagenticpay\\console\\index.html") for name in names)
        for name in names:
            lowered = name.replace("\\", "/").lower()
            for fragment in forbidden_fragments:
                assert fragment.lower() not in lowered, f"{archive.name} contains {name}"
            if Path(name).name in FORBIDDEN_NAMES:
                raise AssertionError(f"{archive.name} contains {name}")
        for name, text in texts:
            match = SECRET_RE.search(text)
            assert match is None, f"{archive.name}:{name} matches {match.group(0)[:12]}..."


def _archive_entries(path: Path):
    names: list[str] = []
    texts: list[tuple[str, str]] = []
    if path.suffix == ".whl" or path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            for name in names:
                if name.endswith("/"):
                    continue
                payload = zf.read(name)
                if b"\x00" in payload[:2048]:
                    continue
                texts.append((name, payload.decode("utf-8", errors="replace")))
    else:
        with tarfile.open(path, "r:gz") as tf:
            names = tf.getnames()
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                payload = extracted.read()
                if b"\x00" in payload[:2048]:
                    continue
                texts.append((member.name, payload.decode("utf-8", errors="replace")))
    return names, texts
