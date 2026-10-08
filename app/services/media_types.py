from __future__ import annotations


# Keep intake, browser downloads, and manifest scanning on the same audio set.
AUDIO_CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
}

SUPPORTED_AUDIO_EXTENSIONS = frozenset(AUDIO_CONTENT_TYPE_EXTENSIONS.values())
FONT_CONTENT_TYPE_EXTENSIONS = {
    "font/ttf": ".ttf", "font/otf": ".otf",
    "application/x-font-ttf": ".ttf", "application/x-font-opentype": ".otf",
    "application/font-sfnt": ".ttf", "application/x-font-truetype": ".ttf",
}
SUPPORTED_FONT_EXTENSIONS = frozenset({".ttf", ".otf"})
