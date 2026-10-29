"""One-shot, local Windows memory handoff. No credential is persisted or logged.

This isolated R69 component does not authorize or launch a provider request.
The caller supplies an independently verified worker PID and creation time.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
import re
import secrets
import struct
import time
from ctypes import wintypes as wt

INVALID_HANDLE = ctypes.c_void_p(-1).value
MAX_PAYLOAD = 4096


class HandoffRejected(Exception):
    """The handoff did not establish the expected local process identity."""


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wt.DWORD),
        ("OffsetHigh", wt.DWORD),
        ("hEvent", wt.HANDLE),
    ]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wt.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wt.BOOL),
    ]


def _api():
    if os.name != "nt":
        raise HandoffRejected("Windows local pipe required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CloseHandle": ([wt.HANDLE], wt.BOOL),
        "LocalFree": ([ctypes.c_void_p], ctypes.c_void_p),
        "GetCurrentProcess": ([], wt.HANDLE),
        "CreateNamedPipeW": (
            [
                wt.LPCWSTR,
                wt.DWORD,
                wt.DWORD,
                wt.DWORD,
                wt.DWORD,
                wt.DWORD,
                wt.DWORD,
                ctypes.POINTER(SECURITY_ATTRIBUTES),
            ],
            wt.HANDLE,
        ),
        "CreateEventW": ([ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR], wt.HANDLE),
        "ConnectNamedPipe": ([wt.HANDLE, ctypes.POINTER(OVERLAPPED)], wt.BOOL),
        "GetOverlappedResult": (
            [wt.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wt.DWORD), wt.BOOL],
            wt.BOOL,
        ),
        "WaitForSingleObject": ([wt.HANDLE, wt.DWORD], wt.DWORD),
        "CancelIoEx": ([wt.HANDLE, ctypes.POINTER(OVERLAPPED)], wt.BOOL),
        "GetNamedPipeClientProcessId": ([wt.HANDLE, ctypes.POINTER(wt.ULONG)], wt.BOOL),
        "GetNamedPipeServerProcessId": ([wt.HANDLE, ctypes.POINTER(wt.ULONG)], wt.BOOL),
        "WriteFile": (
            [
                wt.HANDLE,
                ctypes.c_void_p,
                wt.DWORD,
                ctypes.POINTER(wt.DWORD),
                ctypes.POINTER(OVERLAPPED),
            ],
            wt.BOOL,
        ),
        "ReadFile": (
            [
                wt.HANDLE,
                ctypes.c_void_p,
                wt.DWORD,
                ctypes.POINTER(wt.DWORD),
                ctypes.POINTER(OVERLAPPED),
            ],
            wt.BOOL,
        ),
        "CreateFileW": (
            [
                wt.LPCWSTR,
                wt.DWORD,
                wt.DWORD,
                ctypes.c_void_p,
                wt.DWORD,
                wt.DWORD,
                wt.HANDLE,
            ],
            wt.HANDLE,
        ),
        "OpenProcess": ([wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
        "GetProcessTimes": (
            [
                wt.HANDLE,
                ctypes.POINTER(wt.FILETIME),
                ctypes.POINTER(wt.FILETIME),
                ctypes.POINTER(wt.FILETIME),
                ctypes.POINTER(wt.FILETIME),
            ],
            wt.BOOL,
        ),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


def process_birth(pid: int) -> int:
    """Use kernel creation time as well as PID to exclude PID reuse."""
    api = _api()
    process = api.OpenProcess(0x1000, False, pid)
    if not process:
        raise HandoffRejected("Worker identity unavailable")
    try:
        created, exited, kernel, user = (wt.FILETIME() for _ in range(4))
        if not api.GetProcessTimes(
            process,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise HandoffRejected("Worker creation time unavailable")
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        api.CloseHandle(process)


def _descriptor(api):
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    advapi.OpenProcessToken.restype = wt.BOOL
    advapi.GetTokenInformation.argtypes = [
        wt.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
    ]
    advapi.GetTokenInformation.restype = wt.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wt.LPWSTR),
    ]
    advapi.ConvertSidToStringSidW.restype = wt.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wt.LPCWSTR,
        wt.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wt.BOOL
    token, size, sid_text, descriptor = (
        wt.HANDLE(),
        wt.DWORD(),
        wt.LPWSTR(),
        ctypes.c_void_p(),
    )
    if not advapi.OpenProcessToken(api.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise HandoffRejected("Local user token unavailable")
    try:
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(
            token, 1, buffer, size.value, ctypes.byref(size)
        ):
            raise HandoffRejected("Local user SID unavailable")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise HandoffRejected("Local user SID conversion failed")
        # Protected DACL: only this exact current user's SID. No Everyone ACE.
        sddl = "D:P(A;;GA;;;" + sid_text.value + ")"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise HandoffRejected("Local pipe ACL creation failed")
        return descriptor
    finally:
        if sid_text:
            api.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        api.CloseHandle(token)


def _overlapped(api, handle, invoke, timeout_seconds):
    if not 0.01 <= timeout_seconds <= 30:
        raise ValueError("Bounded local handoff timeout required")
    event = api.CreateEventW(None, True, False, None)
    if not event:
        raise HandoffRejected("Pipe event unavailable")
    overlapped = OVERLAPPED(hEvent=event)
    count = wt.DWORD()
    try:
        success = invoke(ctypes.byref(overlapped), ctypes.byref(count))
        error = 0 if success else ctypes.get_last_error()
        if not success and error == 535:  # ERROR_PIPE_CONNECTED
            return 0
        if not success and error != 997:  # ERROR_IO_PENDING
            raise HandoffRejected("Pipe operation failed")
        if not success:
            if api.WaitForSingleObject(event, max(1, int(timeout_seconds * 1000))) != 0:
                api.CancelIoEx(handle, ctypes.byref(overlapped))
                # Drain cancellation before OVERLAPPED/buffers leave scope.
                api.GetOverlappedResult(
                    handle, ctypes.byref(overlapped), ctypes.byref(count), True
                )
                raise HandoffRejected("Pipe operation timed out")
        if not api.GetOverlappedResult(
            handle, ctypes.byref(overlapped), ctypes.byref(count), False
        ):
            raise HandoffRejected("Pipe operation incomplete")
        return count.value
    finally:
        api.CloseHandle(event)


class MemoryPipe:
    def __init__(self):
        self.api = _api()
        self.name = "\\\\.\\pipe\\DIVE-R69-" + secrets.token_hex(24)
        self.handle = None
        descriptor = _descriptor(self.api)
        attributes = SECURITY_ATTRIBUTES(
            ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False
        )
        try:
            handle = self.api.CreateNamedPipeW(
                self.name,
                3 | 0x00080000 | 0x40000000,  # duplex, first instance, overlapped
                8,  # byte mode, PIPE_REJECT_REMOTE_CLIENTS
                1,
                MAX_PAYLOAD + 64,
                MAX_PAYLOAD + 64,
                5000,
                ctypes.byref(attributes),
            )
            if handle == INVALID_HANDLE:
                raise HandoffRejected("Exclusive local pipe unavailable")
            self.handle = handle
        finally:
            self.api.LocalFree(descriptor)

    def deliver(
        self,
        payload: bytes,
        *,
        expected_pid: int,
        expected_birth: int,
        timeout_seconds: float = 10,
    ) -> bool:
        """Send once, only to the exact worker. Receive a fixed receipt."""
        deadline = time.monotonic() + timeout_seconds

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise HandoffRejected("Pipe operation timed out")
            return value

        try:
            if self.handle is None or not 1 <= len(payload) <= MAX_PAYLOAD:
                raise HandoffRejected("One bounded handoff required")
            _overlapped(
                self.api,
                self.handle,
                lambda over, count: self.api.ConnectNamedPipe(self.handle, over),
                remaining(),
            )
            pid = wt.ULONG()
            if not self.api.GetNamedPipeClientProcessId(self.handle, ctypes.byref(pid)):
                raise HandoffRejected("Pipe client identity unavailable")
            if pid.value != expected_pid or process_birth(pid.value) != expected_birth:
                raise HandoffRejected("Pipe client is not the authorized worker")
            buffer = ctypes.create_string_buffer(
                struct.pack("<I", len(payload)) + payload
            )
            count = _overlapped(
                self.api,
                self.handle,
                lambda over, count: self.api.WriteFile(
                    self.handle, buffer, len(buffer) - 1, count, over
                ),
                remaining(),
            )
            if count != len(buffer) - 1:
                raise HandoffRejected("Incomplete handoff")
            receipt = ctypes.create_string_buffer(32)
            count = _overlapped(
                self.api,
                self.handle,
                lambda over, count: self.api.ReadFile(
                    self.handle, receipt, 32, count, over
                ),
                remaining(),
            )
            if count != 32 or not hmac.compare_digest(
                receipt.raw, hashlib.sha256(payload).digest()
            ):
                raise HandoffRejected("Worker did not acknowledge handoff")
            return True
        finally:
            self.close()

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None


def receive(
    name: str,
    *,
    expected_server_pid: int,
    expected_server_birth: int,
    timeout_seconds: float = 10,
) -> bytearray:
    """Read one bounded frame, verifying the actual pipe server process."""
    if not re.fullmatch(r"\\\\\.\\pipe\\DIVE-R69-[0-9a-f]{48}", name):
        raise HandoffRejected("Unexpected local endpoint")
    api = _api()
    handle = api.CreateFileW(name, 0xC0000000, 0, None, 3, 0x40000000, None)
    if handle == INVALID_HANDLE:
        raise HandoffRejected("Local endpoint unavailable")
    try:
        deadline = time.monotonic() + timeout_seconds

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise HandoffRejected("Pipe operation timed out")
            return value

        pid = wt.ULONG()
        if not api.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
            raise HandoffRejected("Pipe server identity unavailable")
        if (
            pid.value != expected_server_pid
            or process_birth(pid.value) != expected_server_birth
        ):
            raise HandoffRejected("Unexpected pipe server process")

        def read_exact(size):
            output = bytearray()
            while len(output) < size:
                buffer = ctypes.create_string_buffer(size - len(output))
                count = _overlapped(
                    api,
                    handle,
                    lambda over, count: api.ReadFile(
                        handle, buffer, len(buffer), count, over
                    ),
                    remaining(),
                )
                if count == 0:
                    raise HandoffRejected("Truncated handoff")
                output.extend(buffer.raw[:count])
            return output

        size = struct.unpack("<I", read_exact(4))[0]
        if not 1 <= size <= MAX_PAYLOAD:
            raise HandoffRejected("Handoff exceeds bound")
        payload = read_exact(size)
        receipt = ctypes.create_string_buffer(hashlib.sha256(payload).digest())
        if (
            _overlapped(
                api,
                handle,
                lambda over, count: api.WriteFile(handle, receipt, 32, count, over),
                remaining(),
            )
            != 32
        ):
            raise HandoffRejected("Handoff acknowledgement failed")
        return payload
    finally:
        api.CloseHandle(handle)
