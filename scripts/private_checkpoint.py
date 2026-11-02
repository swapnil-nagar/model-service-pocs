import ctypes
import os
from ctypes import wintypes
from pathlib import Path


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _transform(data: bytes, purpose: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("The guarded deployment helper requires Windows DPAPI for its private checkpoint")
    source_buffer = ctypes.create_string_buffer(data)
    purpose_buffer = ctypes.create_string_buffer(purpose)
    source = _Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = _Blob(len(purpose), ctypes.cast(purpose_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_pointer = ctypes.POINTER(_Blob)
    crypt32.CryptProtectData.argtypes = [
        blob_pointer, ctypes.c_wchar_p, blob_pointer,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer,
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        blob_pointer, ctypes.POINTER(ctypes.c_wchar_p), blob_pointer,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer,
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source), None, ctypes.byref(entropy),
            None, None, 1, ctypes.byref(target),
        )
    else:
        ok = crypt32.CryptProtectData(
            ctypes.byref(source), "Model service POC deployment",
            ctypes.byref(entropy), None, None, 1, ctypes.byref(target),
        )
    if not ok:
        raise RuntimeError("The private deployment checkpoint could not be protected or read")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(target.data)


def save(path: Path, content: bytes, purpose: str) -> None:
    encrypted = _transform(content, purpose.encode("utf-8"), decrypt=False)
    with path.open("xb") as stream:
        stream.write(encrypted)


def load(path: Path, purpose: str) -> bytes:
    return _transform(path.read_bytes(), purpose.encode("utf-8"), decrypt=True)
