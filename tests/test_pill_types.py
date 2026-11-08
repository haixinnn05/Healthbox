"""The pill types a fresh Healthbox starts with: cartridge A is white, cartridge B is orange."""
from conftest import PRODUCT_CLASSES

from app.vision import _in_ranges, look_alike_pairs


def classes():
    return [{"class_id": c, "label": label, "cartridge": cart, "color": color, "hsv": hsv}
            for c, label, cart, color, hsv in PRODUCT_CLASSES]


def test_cartridge_a_is_white_and_nothing_shows_blue():
    a = next(c for c in classes() if c["cartridge"] == "A")
    assert a["label"] == "White"
    assert not any(c["label"] == "Blue" for c in classes())


def test_white_range_takes_a_white_pill_but_not_the_dark_tray_or_an_orange_pill():
    white = next(c for c in classes() if c["label"] == "White")["hsv"]["ranges"]
    assert _in_ranges([0, 10, 230], white)      # white pill
    assert not _in_ranges([60, 15, 90], white)  # dark grey tray
    assert not _in_ranges([12, 200, 220], white)  # orange pill


def test_white_and_orange_are_not_look_alikes():
    assert look_alike_pairs(classes()) == []
