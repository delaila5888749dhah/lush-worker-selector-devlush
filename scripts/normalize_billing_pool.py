#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import hashlib
import re
from pathlib import Path

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}

# Only US country tokens are allowed. These may appear as metadata before
# the name, e.g.:
#   US|Ryan Young|235 West Canyon St|Randolph|UT|84064|...
COUNTRY_OR_REGION_CODES = {
    "US",
    "USA",
    "UNITED STATES",
    "UNITED STATES OF AMERICA",
}

# If these appear as standalone pipe-delimited tokens, reject the whole line.
# Note: "CA" is intentionally NOT here because it is also California state.
NON_US_COUNTRY_OR_REGION_CODES = {
    "BR",
    "MX",
    "UK",
    "GB",
    "AU",
    "EU",
    "CANADA",
    "BRAZIL",
    "MEXICO",
    "UNITED KINGDOM",
    "GREAT BRITAIN",
    "AUSTRALIA",
}

CURRENCY_OR_STATUS_TOKENS = {
    "USD", "EUR", "GBP", "CAD", "AUD",
    "LIVE", "DEAD", "VALID", "INVALID", "CHECKED", "APPROVED",
    "PREPAID", "DEBIT", "CREDIT", "CLASSIC", "GOLD", "PLATINUM",
    "VISA", "MASTERCARD", "AMEX", "DISCOVER",
    "UNKNOWN", "NULL", "NONE", "N/A", "NA",
}

INSTITUTION_HINT_RE = re.compile(
    r"\b("
    r"bank|bancorp|federal|savings|credit union|cu|national|financial|"
    r"american express|amex|consumer|commerce|merchants|farmers|"
    r"trust|capital|card services|issuer|prepaid|debit|credit|"
    r"visa|mastercard|discover|arvest|yorkshire|nikolet|nicolet|aig|"
    r"international bank|bankers bank|nodaway valley|first farmers"
    r")\b",
    re.IGNORECASE,
)

EMAIL_RE = re.compile(r"^[^@\s|]+@[^@\s|]+\.[^@\s|]+$")
ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
YEAR_RE = re.compile(r"^(?:20)?\d{2}$")
DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$", re.IGNORECASE)

STREET_HINT_RE = re.compile(
    r"\b("
    r"st|street|ave|avenue|rd|road|dr|drive|ln|lane|blvd|boulevard|"
    r"ct|court|cir|circle|way|pl|place|pkwy|parkway|hwy|highway|"
    r"terrace|ter|trail|trl|loop|apt|unit|suite|ste|"
    r"po box|p\.o\. box|box"
    r")\b",
    re.IGNORECASE,
)

NAME_CHARS_RE = re.compile(r"^[A-Za-zÀ-ỹ'’.\- ]+$")


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def norm_space(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def token_upper(value: str) -> str:
    return norm_space(value).upper()


def canonical_text(value: str) -> str:
    text = norm_space(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return norm_space(text)


def is_email(value: str) -> bool:
    return bool(EMAIL_RE.match(norm_space(value)))


def is_domain_like(value: str) -> bool:
    text = norm_space(value)
    return bool(DOMAIN_RE.match(text))


def is_state(value: str) -> bool:
    return token_upper(value) in US_STATES


def is_us_country_or_region(value: str) -> bool:
    return token_upper(value) in COUNTRY_OR_REGION_CODES


def is_non_us_country_or_region(value: str) -> bool:
    return token_upper(value) in NON_US_COUNTRY_OR_REGION_CODES


def is_currency_or_status(value: str) -> bool:
    return token_upper(value) in CURRENCY_OR_STATUS_TOKENS


def is_zip(value: str) -> bool:
    return bool(ZIP_RE.match(norm_space(value)))


def normalize_zip(value: str) -> str:
    text = norm_space(value)
    return text[:5] if ZIP_RE.match(text) else text


def is_phone(value: str) -> bool:
    d = digits_only(value)
    return len(d) == 10 or (len(d) == 11 and d.startswith("1"))


def normalize_phone(value: str) -> str:
    d = digits_only(value)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def normalize_email(value: str) -> str:
    text = norm_space(value).lower()
    return text if is_email(text) else ""


def is_masked_or_plain_card(value: str) -> bool:
    text = norm_space(value)
    d = digits_only(text)

    if 12 <= len(d) <= 19:
        return True

    if "*" in text and len(d) >= 4:
        return True

    if text.isdigit() and len(text) >= 12:
        return True

    return False


def is_exp_month(value: str) -> bool:
    text = norm_space(value)
    if not text.isdigit():
        return False
    try:
        n = int(text)
    except ValueError:
        return False
    return 1 <= n <= 12


def is_exp_year(value: str) -> bool:
    text = norm_space(value)
    if not YEAR_RE.match(text):
        return False
    try:
        n = int(text[-2:])
    except ValueError:
        return False
    return 24 <= n <= 40


def is_cvv_like(value: str) -> bool:
    text = norm_space(value)
    return text.isdigit() and len(text) in (3, 4)


def is_address_like(value: str) -> bool:
    text = norm_space(value)
    if not text:
        return False

    d = digits_only(text)
    if not d:
        return False

    if STREET_HINT_RE.search(text):
        return True

    return bool(re.match(r"^\d+\s+\S+", text)) and any(ch.isalpha() for ch in text)


def is_bad_name_token(value: str) -> bool:
    text = norm_space(value)

    if not text:
        return True

    if is_email(text):
        return True
    if is_domain_like(text):
        return True
    if is_phone(text):
        return True
    if is_zip(text):
        return True
    if is_state(text):
        return True
    if is_us_country_or_region(text):
        return True
    if is_non_us_country_or_region(text):
        return True
    if is_currency_or_status(text):
        return True
    if INSTITUTION_HINT_RE.search(text):
        return True
    if is_masked_or_plain_card(text):
        return True
    if is_address_like(text):
        return True

    # Reject short uppercase codes like BR, US, CC, BIN, etc.
    if text.isupper() and len(text) <= 4:
        return True

    return False


def is_name_part(value: str, *, allow_short: bool = False) -> bool:
    text = norm_space(value)

    if is_bad_name_token(text):
        return False

    if not NAME_CHARS_RE.match(text):
        return False

    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < (1 if allow_short else 2):
        return False

    # Reject single-letter first names because source is noisy.
    if len(text.replace(" ", "")) < 2 and not allow_short:
        return False

    digit_count = sum(ch.isdigit() for ch in text)
    if digit_count > 0:
        return False

    if len(text) > 40:
        return False

    return True


def split_full_name(value: str) -> tuple[str, str] | None:
    text = norm_space(value)

    if is_bad_name_token(text):
        return None

    if not NAME_CHARS_RE.match(text):
        return None

    words = [w for w in text.split(" ") if w]
    if len(words) < 2:
        return None

    if token_upper(text) in COUNTRY_OR_REGION_CODES:
        return None

    first = words[0]
    last = " ".join(words[1:])

    if not is_name_part(first):
        return None
    if not is_name_part(last):
        return None

    return first, last


def find_email(tokens: list[str]) -> str:
    for t in tokens:
        email = normalize_email(t)
        if email:
            return email
    return ""


def find_phone(tokens: list[str]) -> str:
    for t in tokens:
        phone = normalize_phone(t)
        if phone and not is_masked_or_plain_card(t):
            return phone
    return ""


def find_zip_index(tokens: list[str]) -> int | None:
    for i, t in enumerate(tokens):
        if is_zip(t):
            return i
    return None


def find_state_index(tokens: list[str]) -> int | None:
    for i, t in enumerate(tokens):
        if is_state(t):
            return i
    return None


def find_address_index(tokens: list[str]) -> int | None:
    for i, t in enumerate(tokens):
        if is_address_like(t):
            return i
    return None


def find_city(tokens: list[str], address_i: int | None, state_i: int | None, zip_i: int | None) -> str:
    if state_i is not None and state_i > 0:
        cand = norm_space(tokens[state_i - 1])
        if (
            cand
            and not is_bad_name_token(cand)
            and not is_address_like(cand)
            and any(ch.isalpha() for ch in cand)
        ):
            return cand

    if address_i is not None and zip_i is not None and address_i < zip_i:
        for i in range(address_i + 1, zip_i):
            cand = norm_space(tokens[i])
            if (
                cand
                and not is_state(cand)
                and not is_bad_name_token(cand)
                and any(ch.isalpha() for ch in cand)
            ):
                return cand

    return ""


def find_name_pair(tokens: list[str], address_i: int | None) -> tuple[str, str] | None:
    search_end = address_i if address_i is not None else len(tokens)

    # 1. Prefer full-name token before address:
    #    Full Name|Bank/Issuer|Address|City|State|Zip...
    #    US|Full Name|Address|City|State|Zip...
    for i in range(0, search_end):
        maybe = split_full_name(tokens[i])
        if maybe is not None:
            return maybe

    # 2. Then support adjacent first|last before address:
    #    first|last|address|city|state|zip...
    for i in range(0, max(0, search_end - 1)):
        a = norm_space(tokens[i])
        b = norm_space(tokens[i + 1])
        if is_name_part(a) and is_name_part(b):
            return a, b

    return None


def normalize_line(line: str) -> tuple[str | None, str]:
    raw = line.strip()
    if not raw:
        return None, "empty"

    tokens = [norm_space(part) for part in raw.split("|")]
    tokens = [t for t in tokens if t]

    if len(tokens) < 6:
        return None, "too_few_fields"

    # Reject explicit non-US country/region metadata.
    # "CA" is not treated as non-US here because it is also California.
    for t in tokens:
        if is_non_us_country_or_region(t):
            return None, "non_us_country"

    email = find_email(tokens)
    phone = find_phone(tokens)
    zip_i = find_zip_index(tokens)
    state_i = find_state_index(tokens)
    address_i = find_address_index(tokens)

    if address_i is None:
        return None, "no_address"
    if state_i is None:
        return None, "no_state"
    if zip_i is None:
        return None, "no_zip"

    address = norm_space(tokens[address_i])
    state = token_upper(tokens[state_i])
    zip_code = normalize_zip(tokens[zip_i])
    city = find_city(tokens, address_i, state_i, zip_i)

    if not city:
        return None, "no_city"

    pair = find_name_pair(tokens, address_i)
    if pair is None:
        return None, "no_name_pair"

    first, last = pair

    if not is_name_part(first) or not is_name_part(last):
        return None, "bad_name"

    clean = "|".join([
        first,
        last,
        address,
        city,
        state,
        zip_code,
        phone,
        email,
    ])
    return clean, "accepted"


def exact_profile_key(clean_line: str) -> str:
    return hashlib.sha256(clean_line.encode("utf-8")).hexdigest()


def parse_clean_line(clean_line: str) -> tuple[str, str, str, str, str, str, str, str]:
    parts = clean_line.split("|")
    while len(parts) < 8:
        parts.append("")
    return tuple(parts[:8])  # type: ignore[return-value]


def household_key(clean_line: str) -> str:
    first, last, address, _city, _state, zip_code, _phone, _email = parse_clean_line(clean_line)
    return "|".join([
        canonical_text(first),
        canonical_text(last),
        canonical_text(address),
        normalize_zip(zip_code),
    ])


def phone_key(clean_line: str) -> str:
    *_prefix, phone, _email = parse_clean_line(clean_line)
    return normalize_phone(phone)


def email_key(clean_line: str) -> str:
    *_prefix, email = parse_clean_line(clean_line)
    return normalize_email(email)


class Deduper:
    def __init__(self, *, dedupe_phone: bool, dedupe_email: bool) -> None:
        self.exact_seen: set[str] = set()
        self.household_seen: set[str] = set()
        self.phone_seen: set[str] = set()
        self.email_seen: set[str] = set()
        self.dedupe_phone = dedupe_phone
        self.dedupe_email = dedupe_email

    def check_and_add(self, clean_line: str) -> str | None:
        exact = exact_profile_key(clean_line)
        if exact in self.exact_seen:
            return "duplicate_exact"

        house = household_key(clean_line)
        if house in self.household_seen:
            return "duplicate_household"

        phone = phone_key(clean_line)
        if self.dedupe_phone and phone and phone in self.phone_seen:
            return "duplicate_phone"

        email = email_key(clean_line)
        if self.dedupe_email and email and email in self.email_seen:
            return "duplicate_email"

        self.exact_seen.add(exact)
        self.household_seen.add(house)
        if phone:
            self.phone_seen.add(phone)
        if email:
            self.email_seen.add(email)

        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize mixed pipe-delimited US billing data into "
            "first|last|address|city|state|zip|phone|email."
        )
    )
    parser.add_argument("--source", required=True, help="Source folder containing mixed .txt files.")
    parser.add_argument("--dest", required=True, help="Destination folder for clean billing files.")
    parser.add_argument("--out-name", default="billing_clean.txt", help="Output clean txt filename.")
    parser.add_argument("--reject-name", default="billing_rejects_summary.tsv", help="PII-safe reject summary filename.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write clean output.")
    parser.add_argument(
        "--no-dedupe-phone",
        action="store_true",
        help="Do not reject duplicate phone numbers.",
    )
    parser.add_argument(
        "--no-dedupe-email",
        action="store_true",
        help="Do not reject duplicate emails.",
    )
    args = parser.parse_args()

    source = Path(args.source).resolve()
    dest = Path(args.dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    accepted: list[str] = []
    reject_counts: collections.Counter[str] = collections.Counter()
    file_stats: collections.Counter[str] = collections.Counter()
    deduper = Deduper(
        dedupe_phone=not args.no_dedupe_phone,
        dedupe_email=not args.no_dedupe_email,
    )

    scanned_files = 0
    scanned_lines = 0

    for path in sorted(source.glob("*.txt")):
        # Avoid reading our own output if dest is inside source.
        if dest in path.parents or path.parent == dest:
            continue

        scanned_files += 1
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            reject_counts["file_read_error"] += 1
            file_stats[f"{path.name}\tfile_read_error"] += 1
            continue

        for _line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue

            scanned_lines += 1
            clean, reason = normalize_line(line)
            if clean is None:
                reject_counts[reason] += 1
                file_stats[f"{path.name}\t{reason}"] += 1
                continue

            duplicate_reason = deduper.check_and_add(clean)
            if duplicate_reason is not None:
                reject_counts[duplicate_reason] += 1
                file_stats[f"{path.name}\t{duplicate_reason}"] += 1
                continue

            accepted.append(clean)

    out_path = dest / args.out_name
    reject_path = dest / args.reject_name

    if not args.dry_run:
        out_path.write_text(
            "\n".join(accepted) + ("\n" if accepted else ""),
            encoding="utf-8",
        )

    # PII-safe summary only: file name + reason + count.
    with reject_path.open("w", encoding="utf-8") as fh:
        fh.write("file\treason\tcount\n")
        for key, count in sorted(file_stats.items()):
            file_name, reason = key.split("\t", 1)
            fh.write(f"{file_name}\t{reason}\t{count}\n")

    print(f"source={source}")
    print(f"dest={dest}")
    print(f"scanned_files={scanned_files}")
    print(f"scanned_lines={scanned_lines}")
    print(f"accepted={len(accepted)}")
    print(f"rejected={sum(reject_counts.values())}")
    print("reject_reasons:")
    for reason, count in reject_counts.most_common():
        print(f"  {reason}={count}")

    if not args.dry_run:
        print(f"wrote_clean={out_path}")
    else:
        print("dry_run=true; clean file not written")
    print(f"wrote_reject_summary={reject_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())