"""PWA 아이콘(PNG)을 만든다: 초록 바탕에 흰 집 모양. 외부 이미지 라이브러리 없이 순수 파이썬(zlib)으로 그린다.

실행: uv run python tools/gen_icons.py   (pa/web/static/icons/ 에 쓴다. 테스트가 만든 결과와 저장된 파일이 같은지 확인한다)
"""
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "pa/web/static/icons"
BG = (15, 118, 110)       # design/tokens.json 의 기본 테마 green
FG = (255, 255, 255)
SS = 3                    # 가장자리를 부드럽게 하는 초과 표본(SS x SS)


def _in_poly(x: float, y: float, poly) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _house(scale: float):
    """0~1 좌표계의 집 모양(지붕·몸통·문). scale 로 가운데에 줄여 마스커블 안전 영역을 만든다."""
    def t(pts):
        return [(0.5 + (x - 0.5) * scale, 0.5 + (y - 0.5) * scale) for x, y in pts]
    roof = t([(0.18, 0.50), (0.50, 0.20), (0.82, 0.50), (0.74, 0.50), (0.50, 0.28), (0.26, 0.50)])
    body = t([(0.26, 0.48), (0.50, 0.28), (0.74, 0.48), (0.74, 0.80), (0.26, 0.80)])
    door = t([(0.44, 0.80), (0.44, 0.58), (0.56, 0.58), (0.56, 0.80)])
    return roof, body, door


def _rounded(x: float, y: float, r: float) -> bool:
    cx, cy = min(max(x, r), 1 - r), min(max(y, r), 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render(size: int, *, full_bleed: bool, scale: float = 1.0) -> bytes:
    """RGBA PNG 바이트. full_bleed=True 면 모서리를 깎지 않는다(iOS·마스커블 아이콘은 OS 가 모양을 만든다)."""
    roof, body, door = _house(scale)
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    x, y = (px + (sx + 0.5) / SS) / size, (py + (sy + 0.5) / SS) / size
                    if not full_bleed and not _rounded(x, y, 0.22):
                        continue
                    glyph = (_in_poly(x, y, roof) or _in_poly(x, y, body)) and not _in_poly(x, y, door)
                    cr, cg, cb = FG if glyph else BG
                    r, g, b, a = r + cr, g + cg, b + cb, a + 1
            n = SS * SS
            if a:
                row += bytes((r // a, g // a, b // a, 255 * a // n))
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    return _png(size, size, b"".join(rows))


def _png(w: int, h: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" fill="#0F766E"/>'
       '<path fill="#fff" fill-rule="evenodd" d="M18 50 50 20l32 30h-8L50 28 26 50zM26 48 50 28l24 20v32H26zm18 32V58h12v22z"/></svg>\n')

FILES = {   # 이름 -> (크기, 모서리 깎기 여부, 글자 영역 비율)
    "icon-192.png": (192, False, 1.0), "icon-512.png": (512, False, 1.0),
    "icon-maskable-512.png": (512, True, 0.8), "apple-touch-icon.png": (180, True, 1.0),
}


def build() -> dict[str, bytes]:
    out = {name: render(size, full_bleed=bleed, scale=scale) for name, (size, bleed, scale) in FILES.items()}
    out["icon.svg"] = SVG.encode("utf-8")
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in build().items():
        (OUT / name).write_bytes(data)
        print(f"wrote {name} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
