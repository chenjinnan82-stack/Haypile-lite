"""Read desktop input without depending on any Haypile window."""
import ctypes
import logging
import os
import sys

logger = logging.getLogger(__name__)


class DesktopInputState:
    def __init__(self):
        self._click_counter = None
        self._escape_state = None
        if sys.platform == "darwin" and os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            try:
                framework = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
                self._click_counter = framework.CGEventSourceCounterForEventType
                self._click_counter.argtypes = [ctypes.c_int32, ctypes.c_uint32]
                self._click_counter.restype = ctypes.c_uint32
                self._escape_state = framework.CGEventSourceKeyState
                self._escape_state.argtypes = [ctypes.c_int32, ctypes.c_uint16]
                self._escape_state.restype = ctypes.c_bool
            except (AttributeError, OSError):
                logger.debug("Desktop input state is unavailable")

    def buttons(self):
        if self._click_counter is not None:
            return tuple(self._click_counter(0, event_type) for event_type in (1, 3, 25))
        if sys.platform.startswith("win"):
            return tuple(ctypes.windll.user32.GetAsyncKeyState(key) for key in (1, 2, 4))
        return ()

    def escape(self) -> bool:
        return bool(self._escape_state and self._escape_state(0, 53))
