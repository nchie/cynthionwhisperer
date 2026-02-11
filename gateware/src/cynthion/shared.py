# SPDX-License-Identifier: BSD-3-Clause

"""Shared USB constants used by standalone analyzer gateware."""

from collections import namedtuple


def _dict_to_namedtuple(data, typename="_"):
    return namedtuple(typename, data.keys())(
        *(_dict_to_namedtuple(v, typename + "_" + k) if isinstance(v, dict) else v for k, v in data.items())
    )


usb = _dict_to_namedtuple(
    {
        "bVendorId": {
            "apollo": 0x1D50,
            "cynthion": 0x1D50,
            "example": 0x1209,
        },
        "bProductId": {
            "apollo": 0x615C,
            "cynthion": 0x615B,
            "example": 0x0001,
            "example_2": 0x0002,
            "example_3": 0x0003,
            "example_4": 0x0004,
            "example_5": 0x0005,
            "analyzer_test": 0x000A,
        },
        "bManufacturerString": {
            "apollo": "Great Scott Gadgets",
            "bulk_speed_test": "Luna Project",
            "analyzer": "Cynthion Project",
            "moondancer": "Cynthion Project",
            "example": "https://pid.codes/1209/",
        },
        "bProductString": {
            "apollo": "Cynthion Apollo Debugger",
            "bulk_speed_test": "Bulk Speed Test",
            "analyzer": "USB Analyzer",
            "moondancer": "Facedancer",
            "example": "pid.codes Test PID 1",
            "example_2": "pid.codes Test PID 2",
            "example_3": "pid.codes Test PID 3",
            "example_4": "pid.codes Test PID 4",
            "example_5": "pid.codes Test PID 5",
        },
        "bInterfaceSubClass": {
            "apollo": 0x00,
            "analyzer": 0x10,
            "moondancer": 0x20,
        },
        "bInterfaceProtocol": {
            "analyzer": 0x01,
            "moondancer": 0x00,
        },
    }
)
