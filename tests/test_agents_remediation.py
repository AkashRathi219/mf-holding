"""[T8] Remediation handler tests (SPEC §6 / AC-7, AC-8, AC-10).

Covers the four taxonomy->fix handlers in ``src/agents/remediation.py``:
fraction-scale normalization (fires only when 0.90 <= Σ <= 1.10 and never
double-scales), ISIN/%-density page ranking for 150+-page PDFs (mirrors the
proven ``scripts/linksheet_sync.py`` scoring), holdings-sheet selection for
100+-sheet workbooks, and the OpenSSL ``Salted__`` envelope parse with the
pure ``EVP_BytesToKey`` (MD5) KDF - decryption itself is injected, never
implemented here.  Every handler is exercised pure/injected: no network, no
real PDF, no real AES.  Bad input returns ``(None, {"failure_code": ...})``
and never raises (AC-5: every failure carries a valid taxonomy code).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.agents.remediation import (
    AES_IV_LEN,
    AES_KEY_LEN,
    CODE_ENCRYPTED_PAYLOAD_AES,
    CODE_PDF_HIGH_PAGE_DENSITY,
    CODE_SCALE_FRACTION_NAV,
    CODE_WORKBOOK_MULTI_SHEET,
    decrypt_payload,
    evp_bytes_to_key,
    normalize_scale,
    rank_pdf_pages,
    select_sheets,
)
from src.agents.taxonomy import is_valid_code

DENSE_PAGE = (
    "Name of the Instrument ISIN % to Nav\n"
    "HDFC Bank Ltd INE040A01034 8.25%\n"
    "Reliance Industries Ltd INE002A01018 7.40%\n"
    "Infosys Ltd INE009A01021 6.10%\n"
)
SPARSE_PAGE = "Name of the Instrument ISIN % to Nav\nTata Consultancy Services INE467A01029 4.05%\n"
FILLER_PAGE = "quarterly performance review of the fund manager team " * 3


# ---------------------------------------------------------------------------
# codes are taxonomy codes (AC-5)
# ---------------------------------------------------------------------------

def test_remediation_codes_are_valid_taxonomy_codes():
    for code in (
        CODE_SCALE_FRACTION_NAV,
        CODE_PDF_HIGH_PAGE_DENSITY,
        CODE_WORKBOOK_MULTI_SHEET,
        CODE_ENCRYPTED_PAYLOAD_AES,
    ):
        assert is_valid_code(code)


# ---------------------------------------------------------------------------
# 1-3. normalize_scale (ERR_SCALE_FRACTION_NAV)
# ---------------------------------------------------------------------------

def test_fraction_rows_summing_one_are_scaled_to_percent():
    rows = [
        {"company": "HDFC Bank Ltd", "isin": "INE040A01034", "percent_nav": 0.02},
        {"company": "Reliance Industries", "isin": "INE002A01018", "percent_nav": 0.30},
        {"company": "Infosys Ltd", "isin": "INE009A01021", "percent_nav": 0.68},
    ]
    fixed, info = normalize_scale(rows)
    assert fixed is not None
    assert [row["percent_nav"] for row in fixed] == [2.0, 30.0, 68.0]
    assert rows[0]["percent_nav"] == 0.02
    assert info["scaled"] is True
    assert info["factor"] == 100.0
    assert info["original_total"] == pytest.approx(1.0)
    assert info["new_total"] == pytest.approx(100.0)
    assert "failure_code" not in info


def test_percent_rows_summing_97_are_not_scaled():
    rows = [
        {"company": "HDFC Bank Ltd", "percent_nav": 50.0},
        {"company": "Reliance Industries", "percent_nav": 47.0},
    ]
    fixed, info = normalize_scale(rows)
    assert fixed == rows
    assert [row["percent_nav"] for row in fixed] == [50.0, 47.0]
    assert info["scaled"] is False
    assert info["factor"] == 1.0
    assert info["original_total"] == 97.0
    assert info["new_total"] == 97.0
    assert "failure_code" not in info


def test_already_normalized_never_rescales_fraction_looking_rows():
    rows = [{"company": "A", "percent_nav": 0.5}, {"company": "B", "percent_nav": 0.5}]
    fixed, info = normalize_scale(rows, already_normalized=True)
    assert [row["percent_nav"] for row in fixed] == [0.5, 0.5]
    assert info["scaled"] is False
    assert info["factor"] == 1.0
    assert info["original_total"] == pytest.approx(1.0)
    assert info["new_total"] == pytest.approx(1.0)
    assert "failure_code" not in info


# ---------------------------------------------------------------------------
# 4-5. rank_pdf_pages (ERR_PDF_HIGH_PAGE_DENSITY)
# ---------------------------------------------------------------------------

def test_rank_pdf_pages_picks_highest_isin_density_ascending():
    page_texts = [FILLER_PAGE, SPARSE_PAGE, DENSE_PAGE, FILLER_PAGE, DENSE_PAGE, FILLER_PAGE]
    picked, info = rank_pdf_pages(page_texts, max_pages=2)
    assert picked == [2, 4]
    assert info["fallback"] is False
    assert info["n_pages"] == 6
    assert info["scores"][2] > info["scores"][1]
    assert "failure_code" not in info


def test_rank_pdf_pages_works_on_injected_texts_without_any_pdf(tmp_path: Path):
    page_texts = ["x" * 60, DENSE_PAGE, "y" * 60]
    picked, info = rank_pdf_pages(tmp_path / "no_such_file.pdf", page_texts=page_texts)
    assert picked == [1]
    assert info["n_pages"] == 3
    assert info["fallback"] is False
    assert "failure_code" not in info


# ---------------------------------------------------------------------------
# 6. select_sheets (ERR_WORKBOOK_MULTI_SHEET)
# ---------------------------------------------------------------------------

def test_select_sheets_picks_holdings_sheets_over_cover_and_index():
    sheet_texts = {
        "Cover": "annual report of the fund house for the year ended 31 march " + "z" * 30,
        "Index": "table of contents listing every other sheet in this workbook " + "z" * 30,
        "Equity": (
            "Name of the Instrument ISIN % to Nav\n"
            "HDFC Bank Ltd INE040A01034 8.25%\n"
            "Reliance Industries Ltd INE002A01018 7.40%\n"
        ),
        "Debt": "Name of the Instrument ISIN % to Nav\nTata Consultancy Services INE467A01029 4.05%\n",
        "Notes": "accounting policies and notes to the financial statements " + "z" * 30,
    }
    picks, info = select_sheets(sheet_texts, max_sheets=2)
    assert picks == ["Equity", "Debt"]
    assert info["fallback"] is False
    assert info["scores"]["Equity"] > info["scores"]["Debt"] > 0
    assert set(picks).isdisjoint({"Cover", "Index", "Notes"})
    assert "failure_code" not in info


# ---------------------------------------------------------------------------
# 7. evp_bytes_to_key (pure KDF)
# ---------------------------------------------------------------------------

def test_evp_bytes_to_key_deterministic_right_lengths():
    salt = bytes.fromhex("0102030405060708")
    key, iv = evp_bytes_to_key("session-key", salt, AES_KEY_LEN, AES_IV_LEN)
    assert len(key) == AES_KEY_LEN
    assert len(iv) == AES_IV_LEN
    assert (key, iv) == evp_bytes_to_key("session-key", salt, AES_KEY_LEN, AES_IV_LEN)
    pwd = b"session-key"
    d1 = hashlib.md5(pwd + salt).digest()
    d2 = hashlib.md5(d1 + pwd + salt).digest()
    d3 = hashlib.md5(d2 + pwd + salt).digest()
    assert key == d1 + d2
    assert iv == d3
    key_other, _ = evp_bytes_to_key("session-key", bytes.fromhex("0807060504030201"), AES_KEY_LEN, AES_IV_LEN)
    assert key_other != key
    assert evp_bytes_to_key(pwd, salt, AES_KEY_LEN, AES_IV_LEN) == (key, iv)


# ---------------------------------------------------------------------------
# 8. decrypt_payload (ERR_ENCRYPTED_PAYLOAD_AES)
# ---------------------------------------------------------------------------

def test_decrypt_payload_parses_envelope_and_delegates_to_injected_decryptor():
    salt = bytes.fromhex("0102030405060708")
    ciphertext = bytes(range(16)) + bytes(range(16, 32))
    blob = b"Salted__" + salt + ciphertext
    seen: dict[str, object] = {}

    def decryptor(ct, key, iv):
        seen["ciphertext"] = ct
        seen["key"] = key
        seen["iv"] = iv
        return b'{"schemes": []}'

    plaintext, info = decrypt_payload(blob, "session-key", decryptor=decryptor)
    assert plaintext == b'{"schemes": []}'
    assert seen["ciphertext"] == ciphertext
    expected_key, expected_iv = evp_bytes_to_key("session-key", salt, AES_KEY_LEN, AES_IV_LEN)
    assert seen["key"] == expected_key
    assert seen["iv"] == expected_iv
    assert info["salt_hex"] == salt.hex()
    assert info["plaintext_len"] == len(plaintext)
    assert "failure_code" not in info


def test_decrypt_payload_without_decryptor_returns_typed_failure():
    blob = b"Salted__" + bytes.fromhex("0102030405060708") + bytes(range(16))
    plaintext, info = decrypt_payload(blob, "session-key")
    assert plaintext is None
    assert info["failure_code"] == CODE_ENCRYPTED_PAYLOAD_AES
    assert is_valid_code(info["failure_code"])
    assert info["salt_hex"] == "0102030405060708"


# ---------------------------------------------------------------------------
# 9. bad input -> typed failure, never raises
# ---------------------------------------------------------------------------

def test_bad_input_returns_typed_failure_and_never_raises(tmp_path: Path):
    for bad_rows in (None, [], "rows", [42], [{"company": "A"}], [{"percent_nav": "abc"}], [{"percent_nav": None}]):
        fixed, info = normalize_scale(bad_rows)
        assert fixed is None, bad_rows
        assert info.get("failure_code") == CODE_SCALE_FRACTION_NAV
        assert is_valid_code(info["failure_code"])
        assert info.get("error")

    for bad_source in (None, 123, [], [None], [1, 2], tmp_path / "missing.pdf"):
        picked, info = rank_pdf_pages(bad_source)
        assert picked is None, bad_source
        assert info.get("failure_code") == CODE_PDF_HIGH_PAGE_DENSITY
        assert is_valid_code(info["failure_code"])
        assert info.get("error")

    for bad_source in (None, 123, [], {}, {"S": 42}, tmp_path / "missing.xlsx"):
        picks, info = select_sheets(bad_source)
        assert picks is None, bad_source
        assert info.get("failure_code") == CODE_WORKBOOK_MULTI_SHEET
        assert is_valid_code(info["failure_code"])
        assert info.get("error")

    valid_blob = b"Salted__" + bytes(8) + bytes(16)
    for bad_blob in (None, "Salted__text", b"", b"NOTSALT!" + bytes(24), b"Salted__" + bytes(8),
                     b"Salted__" + bytes(8) + bytes(5), b"Salted__" + bytes(8) + bytes(20)):
        plaintext, info = decrypt_payload(bad_blob, "pw")
        assert plaintext is None, bad_blob
        assert info.get("failure_code") == CODE_ENCRYPTED_PAYLOAD_AES
        assert is_valid_code(info["failure_code"])
        assert info.get("error")

    plaintext, info = decrypt_payload(valid_blob, "pw", decryptor=42)
    assert plaintext is None
    assert info.get("failure_code") == CODE_ENCRYPTED_PAYLOAD_AES

    def raising_decryptor(ct, key, iv):
        raise RuntimeError("cipher exploded")

    plaintext, info = decrypt_payload(valid_blob, "pw", decryptor=raising_decryptor)
    assert plaintext is None
    assert info.get("failure_code") == CODE_ENCRYPTED_PAYLOAD_AES

    plaintext, info = decrypt_payload(valid_blob, "pw", decryptor=lambda ct, key, iv: None)
    assert plaintext is None
    assert info.get("failure_code") == CODE_ENCRYPTED_PAYLOAD_AES

    plaintext, info = decrypt_payload(valid_blob, 12345, decryptor=lambda ct, key, iv: ct)
    assert plaintext is None
    assert info.get("failure_code") == CODE_ENCRYPTED_PAYLOAD_AES
