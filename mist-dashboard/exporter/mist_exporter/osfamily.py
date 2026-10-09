"""One short list of operating systems, so Mist's and Aruba's different labels can be charted together.

Mist says "macOS Catalina" or "iOS 16"; an Aruba controller says "macOS", "iPhone" or "Win 11". Both fold into
the same families. Anything that names no known family is "Other"; no answer at all is "Unknown".
"""
FAMILIES = ("macOS", "iOS", "Windows", "Android", "Linux", "ChromeOS", "Other", "Unknown")

_RULES = (
    ("macOS", ("macos", "mac os", "os x", "osx", "mac")),
    ("iOS", ("ios", "ipados", "iphone", "ipad", "ipod")),
    ("Windows", ("win",)),
    ("Android", ("android",)),
    ("ChromeOS", ("chrome",)),
    ("Linux", ("linux", "ubuntu", "debian", "fedora")),
)


def os_family(raw):
    name = str(raw if raw is not None else "").strip().lower()
    if name in ("", "unknown", "none", "n/a"):
        return "Unknown"
    for family, prefixes in _RULES:
        if name.startswith(prefixes):
            return family
    return "Other"
