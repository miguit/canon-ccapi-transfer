import io
from fractions import Fraction

from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from canont import exif


def _jpeg(tags: dict, sub: dict) -> bytes:
    im = Image.new("RGB", (32, 24))
    ex = im.getexif()
    for k, v in tags.items():
        ex[k] = v
    ifd = ex.get_ifd(0x8769)
    for k, v in sub.items():
        ifd[k] = IFDRational(v.numerator, v.denominator) if isinstance(v, Fraction) else v
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=ex)
    return buf.getvalue()


def test_summary_of_canon_like_exif():
    data = _jpeg({0x0110: "Canon EOS M50m2"}, {
        0x829A: Fraction(1, 160), 0x829D: Fraction(9, 2), 0x8822: 2, 0x8827: 10000,
        0x9003: "2026:11:20 22:12:49", 0x9204: Fraction(-2, 3), 0x920A: Fraction(17, 1),
    })
    assert exif.summary(data) == {
        "shutter": "1/160 s", "aperture": "f/4,5", "iso": "ISO 10000", "focal": "17 mm",
        "ev": "−2/3 EV", "mode": "P", "taken": "2026:11:20 22:12:49", "model": "Canon EOS M50m2",
    }


def test_formatting_and_missing_exif():
    assert exif._shutter(Fraction(13, 10)) == "1,3 s"
    assert exif._shutter(Fraction(1, 2)) == "0,5 s"
    assert exif._shutter(Fraction(30)) == "30 s"
    assert exif._ev(Fraction(4, 3)) == "+1 1/3 EV" and exif._ev(Fraction(0)) == "±0 EV"
    plain = io.BytesIO()
    Image.new("RGB", (8, 8)).save(plain, "JPEG")
    assert exif.summary(plain.getvalue()) == {}
    assert exif.summary(b"not a jpeg") == {}
