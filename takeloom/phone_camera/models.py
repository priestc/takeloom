"""Which phone a Takeloom Camera is: its marketing model name.

iOS apps only get a generic "iPhone" for the device's name (iOS 16+ keeps
the user-set name private), so the iOS app sends its hardware identifier
(utsname's machine, e.g. "iPhone14,4") as the hello's "device_model", and
this table turns that into "iPhone 13 mini". Kept here rather than in the
app, so a new model needs a takeloom update, not a camera app release. An
identifier missing from the table shows as-is.

The Android app sends a readable name itself: the device's own marketing
name where Android has one (Settings.Global "device_name", e.g. "Galaxy S23
Ultra"), else manufacturer + model ("Google Pixel 8 Pro").
"""

from __future__ import annotations

_APPLE_MODELS = {
    "iPhone10,1": "iPhone 8", "iPhone10,4": "iPhone 8",
    "iPhone10,2": "iPhone 8 Plus", "iPhone10,5": "iPhone 8 Plus",
    "iPhone10,3": "iPhone X", "iPhone10,6": "iPhone X",
    "iPhone11,2": "iPhone XS",
    "iPhone11,4": "iPhone XS Max", "iPhone11,6": "iPhone XS Max",
    "iPhone11,8": "iPhone XR",
    "iPhone12,1": "iPhone 11",
    "iPhone12,3": "iPhone 11 Pro",
    "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone12,8": "iPhone SE (2nd generation)",
    "iPhone13,1": "iPhone 12 mini",
    "iPhone13,2": "iPhone 12",
    "iPhone13,3": "iPhone 12 Pro",
    "iPhone13,4": "iPhone 12 Pro Max",
    "iPhone14,4": "iPhone 13 mini",
    "iPhone14,5": "iPhone 13",
    "iPhone14,2": "iPhone 13 Pro",
    "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,6": "iPhone SE (3rd generation)",
    "iPhone14,7": "iPhone 14",
    "iPhone14,8": "iPhone 14 Plus",
    "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone15,4": "iPhone 15",
    "iPhone15,5": "iPhone 15 Plus",
    "iPhone16,1": "iPhone 15 Pro",
    "iPhone16,2": "iPhone 15 Pro Max",
    "iPhone17,3": "iPhone 16",
    "iPhone17,4": "iPhone 16 Plus",
    "iPhone17,1": "iPhone 16 Pro",
    "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone17,5": "iPhone 16e",
    "iPhone18,3": "iPhone 17",
    "iPhone18,4": "iPhone Air",
    "iPhone18,1": "iPhone 17 Pro",
    "iPhone18,2": "iPhone 17 Pro Max",
    # iPads (the app runs on them too) — recent models.
    "iPad13,18": "iPad (10th generation)", "iPad13,19": "iPad (10th generation)",
    "iPad14,1": "iPad mini (6th generation)", "iPad14,2": "iPad mini (6th generation)",
    "iPad16,1": "iPad mini (A17 Pro)", "iPad16,2": "iPad mini (A17 Pro)",
    "iPad13,16": "iPad Air (5th generation)", "iPad13,17": "iPad Air (5th generation)",
    "iPad14,8": "iPad Air 11-inch (M2)", "iPad14,9": "iPad Air 11-inch (M2)",
    "iPad14,10": "iPad Air 13-inch (M2)", "iPad14,11": "iPad Air 13-inch (M2)",
    "iPad16,3": "iPad Pro 11-inch (M4)", "iPad16,4": "iPad Pro 11-inch (M4)",
    "iPad16,5": "iPad Pro 13-inch (M4)", "iPad16,6": "iPad Pro 13-inch (M4)",
}

# What the apps report as the device's own name when nobody's set one
# (iOS 16+ only ever gives these): saying nothing beyond the model.
_GENERIC_NAMES = {"", "iphone", "ipad", "ipod touch"}


def model_name(platform: str, device_model: str) -> str:
    """The phone's marketing name from the hello's device_model ("" if the
    app didn't send one: an older build)."""
    device_model = device_model.strip()
    if platform == "ios":
        return _APPLE_MODELS.get(device_model, device_model)
    return device_model


def display_name(client_name: str, platform: str, device_model: str) -> str:
    """How a phone is listed: its model when its name says nothing more
    ("iPhone" -> "iPhone 13 mini"), else its name with the model alongside
    ("Drum cam (iPhone 13 mini)")."""
    model = model_name(platform, device_model)
    name = client_name.strip()
    if not model or name.lower() == model.lower():
        return name or model
    if name.lower() in _GENERIC_NAMES:
        return model
    return f"{name} ({model})"
