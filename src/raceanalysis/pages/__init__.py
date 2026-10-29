# SPDX-License-Identifier: GPL-3.0-or-later
"""Race Analysis tab pages. Each page draws from the window's shared state
(win.a, win.focus, win.compare, win.cursor_d) and has:
    refresh()        everything changed (sessions, reference, turns)
    laps_changed()   which laps are shown / followed changed
    cursor(d)        the shared cursor moved to d metres
"""
