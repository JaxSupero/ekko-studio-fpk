"""fnOS icon silhouette: alpha channel only, no official logo or colors.

Extracted from ICON_256.psd in the official developer template:
https://static.fnnas.com/appcenter-marketing/fnpack_ICON_256.zip
PSD SHA256: afc5c5182689c0a5e9216c2ed8efdb2a6c3e9547efcdecce62543a96a2c37103
The small lossless PNG is embedded to keep text-only source publication portable.
Do not substitute a generic rounded rectangle: fnOS uses a continuous curve.
"""
import base64
import io
from PIL import Image

MASK_PNG_SHA256 = '1028286ca6d6bed2d33ef8a26efb7400295cc7968a976ad0580c51b74db21404'
_MASK_PNG_BASE64 = (
    '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'
    'A3XgTWyIxL9mAAAAAElFTkSuQmCC'
)


def mask_256():
    with Image.open(io.BytesIO(base64.b64decode(_MASK_PNG_BASE64))) as mask:
        return mask.convert('L')
