"""Limpieza cosmética de títulos; no altera IDs, duraciones ni orden."""
import html
import re
import unicodedata

CONNECTORS = set("a al de del el en la las lo los un una y o con por para the of and in on to".split())
ACRONYMS = set("NBA NFL FIFA UEFA UFC WRC TC TV HD UHD LED LCD CPU GPU USB GPS PC IA AI API HTML CSS SQL PHP DJ AC DC USA UK BBC NASA BMW VW YPF".split())


def normalize_title(raw):
    text = html.unescape(html.unescape(raw))
    def clean(char):
        code = ord(char)
        if (0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF or
                0x2300 <= code <= 0x23FF or 0xFE00 <= code <= 0xFE0F or
                0xE0000 <= code <= 0xE007F or code in {0x200D, 0x20E3, 0xA9, 0xAE, 0x2122}):
            return " "
        if char.isspace():
            return " "
        return "" if unicodedata.category(char) == "Cc" else char
    text = "".join(map(clean, text)).replace("*", " ").replace("|", "·")
    text = re.sub(r"\s+", " ", text).strip(" ·-_")
    first = True
    def word(match):
        nonlocal first
        token = match[0]
        letters = "".join(char for char in token if char.isalpha())
        result = token
        if (letters and letters.isupper() and (len(letters) > 1 or token.lower() in CONNECTORS)
                and token not in ACRONYMS and not any(char.isdigit() for char in token)):
            lower = token.lower()
            result = lower if not first and lower in CONNECTORS else lower[0].upper() + lower[1:]
        first = False
        return result
    text = re.sub(r"[^\W_]+", word, text)
    return text[:1].upper() + text[1:]
