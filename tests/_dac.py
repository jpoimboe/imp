"""Drop CAP_DAC_OVERRIDE/READ_SEARCH so uid-0-in-userns tests see real DAC errors."""

from __future__ import annotations

import ctypes
import ctypes.util
import platform
from collections.abc import Iterator
from contextlib import contextmanager

_LINUX_CAPABILITY_VERSION_3 = 0x20080522
_CAP_DAC_OVERRIDE = 1
_CAP_DAC_READ_SEARCH = 2
_MASK = (1 << _CAP_DAC_OVERRIDE) | (1 << _CAP_DAC_READ_SEARCH)


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32),
                ("permitted", ctypes.c_uint32),
                ("inheritable", ctypes.c_uint32)]


_CapData2 = _CapData * 2

if platform.system() == "Linux":
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    _libc.capget.argtypes = [ctypes.POINTER(_CapHeader), ctypes.POINTER(_CapData)]
    _libc.capget.restype = ctypes.c_int
    _libc.capset.argtypes = [ctypes.POINTER(_CapHeader), ctypes.POINTER(_CapData)]
    _libc.capset.restype = ctypes.c_int


def _capget() -> ctypes.Array[_CapData]:
    hdr = _CapHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = _CapData2()
    if _libc.capget(ctypes.byref(hdr), data) != 0:
        raise OSError(ctypes.get_errno(), "capget failed")
    return data


def _capset(data: ctypes.Array[_CapData]) -> None:
    hdr = _CapHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    if _libc.capset(ctypes.byref(hdr), data) != 0:
        raise OSError(ctypes.get_errno(), "capset failed")


@contextmanager
def dac_enforced() -> Iterator[None]:
    if platform.system() != "Linux":
        yield
        return
    data = _capget()
    if not (data[0].effective & _MASK):
        yield
        return
    saved = data[0].effective
    data[0].effective = saved & ~_MASK
    _capset(data)
    try:
        yield
    finally:
        data[0].effective = saved
        _capset(data)
