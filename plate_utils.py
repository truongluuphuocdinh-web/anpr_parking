"""Tiện ích biển số: chuẩn hóa, kiểm tra định dạng, Levenshtein (thuần Python)."""
import re

DIGIT_FIX = {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"}
LETTER_FIX = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G"}
# 59D1-123.45 | 51F-123.45 | 29MD-123.45 (sau khi bỏ dấu '-' và '.')
PLATE_RE = re.compile(r"^\d{2}[A-Z]{1,2}\d?\d{4,5}$")


def to_key(text: str) -> str:
    """Chỉ giữ chữ hoa và số: '59d1-123.45' -> '59D112345'."""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def correct_chars(key: str) -> str:
    """Sửa nhầm lẫn OCR theo vị trí: 2 ký tự đầu và 4 ký tự cuối phải là số, ký tự thứ 3 là chữ."""
    if len(key) < 7:
        return key
    c = list(key)
    for i in (0, 1):
        c[i] = DIGIT_FIX.get(c[i], c[i])
    if c[2].isdigit():
        c[2] = LETTER_FIX.get(c[2], c[2])
    for i in range(len(c) - 4, len(c)):
        c[i] = DIGIT_FIX.get(c[i], c[i])
    return "".join(c)


def normalize(raw: str) -> str:
    return correct_chars(to_key(raw))


def is_valid(key: str) -> bool:
    return bool(PLATE_RE.match(key or ""))


def pretty(key: str) -> str:
    """'59D112345' -> '59D1-123.45'."""
    if not is_valid(key):
        return key
    if len(key) == 9:
        return f"{key[:4]}-{key[4:7]}.{key[7:]}"
    return f"{key[:3]}-{key[3:6]}.{key[6:]}"


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
