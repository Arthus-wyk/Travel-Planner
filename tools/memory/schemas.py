from typing import TypedDict


class PreferenceResult(TypedDict, total=False):
    success: bool
    message: str
