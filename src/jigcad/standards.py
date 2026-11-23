"""Engineering standard tables (metric fasteners, process rules).

Values follow ISO 273 (clearance), ISO 4762 (socket head cap screw),
ISO 10642 (countersunk), ISO 4032 (hex nut) and common heat-set insert
vendor recommendations. Units: mm.
"""
from __future__ import annotations

# size: tap, clr_close, clr_normal, clr_loose, head_d, head_h, cbore_d, csk_d, nut_af, nut_h, insert_d, insert_depth
_RAW = {
    "M2":   (1.6, 2.2, 2.4, 2.6, 3.8, 2.0, 4.4, 4.4, 4.0, 1.6, 3.2, 3.0),
    "M2.5": (2.05, 2.7, 2.9, 3.1, 4.5, 2.5, 5.5, 5.5, 5.0, 2.0, 3.6, 4.0),
    "M3":   (2.5, 3.2, 3.4, 3.6, 5.5, 3.0, 6.5, 6.72, 5.5, 2.4, 4.0, 5.0),
    "M4":   (3.3, 4.3, 4.5, 4.8, 7.0, 4.0, 8.0, 8.96, 7.0, 3.2, 5.6, 6.0),
    "M5":   (4.2, 5.3, 5.5, 5.8, 8.5, 5.0, 10.0, 11.2, 8.0, 4.7, 6.4, 7.0),
    "M6":   (5.0, 6.4, 6.6, 7.0, 10.0, 6.0, 11.0, 13.44, 10.0, 5.2, 8.0, 8.0),
    "M8":   (6.8, 8.4, 9.0, 10.0, 13.0, 8.0, 15.0, 17.92, 13.0, 6.8, 9.6, 10.0),
    "M10":  (8.5, 10.5, 11.0, 12.0, 16.0, 10.0, 18.0, 22.4, 16.0, 8.4, 12.0, 12.0),
    "M12":  (10.2, 13.0, 13.5, 14.5, 18.0, 12.0, 20.0, 26.88, 18.0, 10.8, 14.0, 14.0),
}
_KEYS = ("tap", "clr_close", "clr_normal", "clr_loose", "head_d", "head_h", "cbore_d",
         "csk_d", "nut_af", "nut_h", "insert_d", "insert_depth")

METRIC: dict[str, dict[str, float]] = {k: dict(zip(_KEYS, v)) for k, v in _RAW.items()}

PROCESS = {
    "fdm": {"min_wall": 1.2, "note": "FDM 3D printing (0.4 mm nozzle)"},
    "sla": {"min_wall": 0.8, "note": "resin printing"},
    "cnc": {"min_wall": 1.0, "note": "CNC milling, aluminium"},
    "laser": {"min_wall": 1.0, "note": "laser cut sheet"},
    "generic": {"min_wall": 1.0, "note": "no process specific rules"},
}


def fastener(size: str) -> dict[str, float]:
    s = size.upper().replace("M", "M", 1)
    if s not in METRIC:
        raise ValueError(f"unknown fastener '{size}', known: {', '.join(METRIC)}")
    return METRIC[s]


def clearance(size: str, fit: str = "normal") -> float:
    f = fastener(size)
    key = {"close": "clr_close", "normal": "clr_normal", "loose": "clr_loose",
           "tap": "tap", "insert": "insert_d"}.get(fit)
    if key is None:
        raise ValueError("fit must be one of close|normal|loose|tap|insert")
    return f[key]
