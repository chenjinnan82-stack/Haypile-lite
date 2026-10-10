"""Copy-only dragging with Cocoa's source completion result."""

from __future__ import annotations

import ctypes

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication


class _NSPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def execute_copy_drag(drag, window) -> bool:
    if QGuiApplication.platformName() != "cocoa":
        return drag.exec(Qt.DropAction.CopyAction) == Qt.DropAction.CopyAction

    # Qt's Cocoa backend can return IgnoreAction after an external copy.
    # Observe the native result only for this view and only during this drag.
    try:
        objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        for name, arguments, result in (
            ("object_getClass", [ctypes.c_void_p], ctypes.c_void_p),
            ("sel_registerName", [ctypes.c_char_p], ctypes.c_void_p),
            ("class_getInstanceMethod", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
            ("method_getImplementation", [ctypes.c_void_p], ctypes.c_void_p),
            ("method_getTypeEncoding", [ctypes.c_void_p], ctypes.c_char_p),
            ("method_setImplementation", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
        ):
            function = getattr(objc, name)
            function.argtypes, function.restype = arguments, result
        view = int(window.winId())
        selector = objc.sel_registerName(b"draggingSession:endedAtPoint:operation:")
        method = objc.class_getInstanceMethod(objc.object_getClass(view), selector)
        # ponytail: bridge only the verified 64-bit Qt ABI; re-verify before widening it.
        if not method or objc.method_getTypeEncoding(method) != b"v48@0:8@16{CGPoint=dd}24Q40":
            raise ValueError("unsupported_native_drag_callback")
        signature = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_void_p, _NSPoint, ctypes.c_ulong)
        implementation = objc.method_getImplementation(method)
        if not implementation:
            raise ValueError("missing_native_drag_callback")
        original = signature(implementation)
    except (AttributeError, OSError, TypeError, ValueError):
        return drag.exec(Qt.DropAction.CopyAction) == Qt.DropAction.CopyAction

    native_operation = None

    @signature
    def ended(receiver, command, session, point, operation):
        nonlocal native_operation
        if receiver == view:
            native_operation = operation
        original(receiver, command, session, point, operation)

    saved = objc.method_setImplementation(method, ctypes.cast(ended, ctypes.c_void_p))
    try:
        result = drag.exec(Qt.DropAction.CopyAction)
    finally:
        objc.method_setImplementation(method, saved)
    return result == Qt.DropAction.CopyAction or native_operation == 1  # NSDragOperationCopy
